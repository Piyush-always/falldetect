/*
 * PDM microphone recorder for the XIAO nRF52840 Sense.
 *
 * Streams 16 kHz / 16-bit / mono PCM to a host over USB CDC as framed binary,
 * under host control. Companion host tool: tools/bench/mic_gui.py.
 *
 * This is a bench instrument, not wearable firmware. It shares no code with the
 * fall detector.
 *
 * Execution contexts
 * ------------------
 *   main thread : owns the DMIC, builds frames, produces into tx_rb.
 *   UART ISR    : consumes tx_rb into the CDC FIFO, and takes command bytes in.
 *                 Short, non-blocking, no allocation, no logging.
 *
 * tx_rb and cmd_rb are each single-producer/single-consumer, which Zephyr's
 * ring_buf supports without locking. Frames are size-checked before being
 * written so a frame is never emitted partially - a torn frame would
 * desynchronise the host, whereas a whole dropped frame is merely a counted,
 * recoverable loss.
 *
 * Wire format (little-endian):
 *   0..3  magic 'F','D','A','1'
 *   4     type
 *   5..6  seq   (wraps at 65536)
 *   7..8  len   (payload bytes)
 *   9..   payload
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/audio/dmic.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/sys/byteorder.h>

/* --- audio format --------------------------------------------------------- */

#define SAMPLE_RATE_HZ 16000U
#define SAMPLE_BITS    16U
#define CHANNELS       1U

/* 20 ms blocks: small enough to stop promptly, large enough that the 9-byte
 * frame header is ~1.4% overhead.
 */
#define BLOCK_MS      20U
#define BLOCK_SAMPLES ((SAMPLE_RATE_HZ * BLOCK_MS) / 1000U)          /* 320   */
#define BLOCK_BYTES   (BLOCK_SAMPLES * sizeof(int16_t) * CHANNELS)   /* 640   */
#define BLOCK_COUNT   8U                                             /* 160 ms */

#define READ_TIMEOUT_MS 200U

K_MEM_SLAB_DEFINE_STATIC(mem_slab, BLOCK_BYTES, BLOCK_COUNT, 4);

/* --- framing -------------------------------------------------------------- */

#define FRAME_HDR_LEN 9U

#define FRAME_AUDIO 0x01U
#define FRAME_TEXT  0x02U
#define FRAME_START 0x03U
#define FRAME_STOP  0x04U

/* 8 KB absorbs ~250 ms of host stall on top of the 160 ms held in the slab. */
static uint8_t tx_storage[8192];
static struct ring_buf tx_rb;

static uint8_t cmd_storage[16];
static struct ring_buf cmd_rb;

static const struct device *const cdc = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
static const struct device *const dmic = DEVICE_DT_GET(DT_NODELABEL(dmic_dev));

static uint16_t tx_seq;
static uint32_t dropped;

/* --- UART ISR ------------------------------------------------------------- */

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t c;

			while (uart_fifo_read(dev, &c, 1) == 1) {
				/* Full command buffer means the host is spamming
				 * commands; dropping them is harmless.
				 */
				(void)ring_buf_put(&cmd_rb, &c, 1);
			}
		}

		if (uart_irq_tx_ready(dev)) {
			uint8_t *data;
			uint32_t claim = ring_buf_get_claim(&tx_rb, &data, 64);

			if (claim == 0U) {
				uart_irq_tx_disable(dev);
				(void)ring_buf_get_finish(&tx_rb, 0);
			} else {
				int sent = uart_fifo_fill(dev, data, claim);

				(void)ring_buf_get_finish(&tx_rb,
							  sent > 0 ? (uint32_t)sent : 0U);
			}
		}
	}
}

/* --- frame emission (main thread) ----------------------------------------- */

/* Returns false if the frame did not fit; the caller counts that as a drop.
 *
 * Checking space before writing is safe against the ISR: the consumer only ever
 * frees space, so a successful check cannot later become false.
 */
static bool frame_send(uint8_t type, const void *payload, uint16_t len)
{
	uint8_t hdr[FRAME_HDR_LEN];

	if (ring_buf_space_get(&tx_rb) < (uint32_t)(FRAME_HDR_LEN + len)) {
		return false;
	}

	hdr[0] = 'F';
	hdr[1] = 'D';
	hdr[2] = 'A';
	hdr[3] = '1';
	hdr[4] = type;
	sys_put_le16(tx_seq, &hdr[5]);
	sys_put_le16(len, &hdr[7]);

	(void)ring_buf_put(&tx_rb, hdr, FRAME_HDR_LEN);
	if (len > 0U) {
		(void)ring_buf_put(&tx_rb, payload, len);
	}

	tx_seq++;
	uart_irq_tx_enable(cdc);

	return true;
}

static void text_send(const char *msg)
{
	(void)frame_send(FRAME_TEXT, msg, (uint16_t)strlen(msg));
}

/* --- capture control ------------------------------------------------------ */

static int capture_start(void)
{
	uint8_t pl[8];
	int err;

	err = dmic_trigger(dmic, DMIC_TRIGGER_START);
	if (err < 0) {
		text_send("ERR: dmic start failed");
		return err;
	}

	tx_seq = 0U;
	dropped = 0U;

	sys_put_le32(SAMPLE_RATE_HZ, &pl[0]);
	sys_put_le16(SAMPLE_BITS, &pl[4]);
	sys_put_le16(CHANNELS, &pl[6]);
	(void)frame_send(FRAME_START, pl, sizeof(pl));

	return 0;
}

static int capture_stop(void)
{
	uint8_t pl[4];
	int err;

	err = dmic_trigger(dmic, DMIC_TRIGGER_STOP);

	sys_put_le32(dropped, pl);
	(void)frame_send(FRAME_STOP, pl, sizeof(pl));

	if (err < 0) {
		text_send("ERR: dmic stop failed");
	}

	return err;
}

/* --- main ----------------------------------------------------------------- */

int main(void)
{
	struct pcm_stream_cfg stream = {
		.pcm_width  = SAMPLE_BITS,
		.pcm_rate   = SAMPLE_RATE_HZ,
		.block_size = BLOCK_BYTES,
		.mem_slab   = &mem_slab,
	};
	struct dmic_cfg cfg = {
		.io = {
			/* Limits the PDM clock the driver may pick to what the
			 * MSM261D3526H1CPM supports. Values taken from Zephyr's
			 * dmic sample for this board.
			 */
			.min_pdm_clk_freq = 1000000,
			.max_pdm_clk_freq = 3500000,
			.min_pdm_clk_dc   = 40,
			.max_pdm_clk_dc   = 60,
		},
		.streams = &stream,
		.channel = {
			.req_num_streams = 1,
			.req_num_chan    = CHANNELS,
		},
	};
	bool recording = false;
	int err;

	cfg.channel.req_chan_map_lo = dmic_build_channel_map(0, 0, PDM_CHAN_LEFT);

	ring_buf_init(&tx_rb, sizeof(tx_storage), tx_storage);
	ring_buf_init(&cmd_rb, sizeof(cmd_storage), cmd_storage);

	if (!device_is_ready(cdc)) {
		return -ENODEV;
	}
	if (!device_is_ready(dmic)) {
		return -ENODEV;
	}

	uart_irq_callback_user_data_set(cdc, uart_isr, NULL);
	uart_irq_rx_enable(cdc);

	/* Wait for the host to open the port, so the START frame is not sent
	 * into a void. If line control is unavailable, proceed rather than hang.
	 */
	while (true) {
		uint32_t dtr = 0;

		if (uart_line_ctrl_get(cdc, UART_LINE_CTRL_DTR, &dtr) != 0 || dtr != 0U) {
			break;
		}
		k_msleep(100);
	}

	err = dmic_configure(dmic, &cfg);
	if (err < 0) {
		text_send("ERR: dmic_configure failed");
		return err;
	}

	text_send("mic_record ready");

	while (true) {
		void *buf;
		uint32_t size;
		uint8_t cmd;

		while (ring_buf_get(&cmd_rb, &cmd, 1) == 1U) {
			switch (cmd) {
			case 'S':
				if (!recording && capture_start() == 0) {
					recording = true;
				}
				break;
			case 'X':
				if (recording) {
					(void)capture_stop();
					recording = false;
				}
				break;
			case 'P':
				text_send(recording ? "pong recording" : "pong idle");
				break;
			default:
				/* Ignore anything else: line noise and stray
				 * newlines from a terminal are not errors.
				 */
				break;
			}
		}

		if (!recording) {
			k_msleep(10);
			continue;
		}

		err = dmic_read(dmic, 0, &buf, &size, READ_TIMEOUT_MS);
		if (err < 0) {
			/* -EAGAIN is an idle timeout, not a fault. */
			if (err != -EAGAIN) {
				text_send("ERR: dmic_read failed");
				(void)capture_stop();
				recording = false;
			}
			continue;
		}

		if (!frame_send(FRAME_AUDIO, buf, (uint16_t)size)) {
			dropped++;
		}

		k_mem_slab_free(&mem_slab, buf);
	}

	return 0;
}
