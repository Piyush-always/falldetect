/*
 * falldetect-gkl — labelled data-collection rig.
 *
 * Streams every IMU sample over USB CDC so tools/datalog_gui.py can record
 * labelled activity sessions. This corpus is the input to everything
 * downstream: the activity classifier, the fall thresholds, and the validation
 * of both. Nothing here detects anything — it only captures, faithfully.
 *
 * Wire format, one line per sample:
 *     $D,<seq>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
 *     $P,<seq>,<steps>          hardware step counter, 1 Hz
 *     $I,<odr>,<accel_fs_g>,<gyro_fs_dps>   identity, on connect
 *
 * <seq> increments once per sample and is the whole point: it is how the host
 * detects that data went missing. A gap in a recording is otherwise invisible,
 * and a corpus with silent holes produces a model with silent holes.
 *
 * Units: accel milli-g, gyro deci-dps. Integer maths only.
 *
 * Execution contexts
 * ------------------
 *   lsm6dsl trigger thread : reads the sample, formats it, produces into tx_rb.
 *   main thread            : pedometer poll and identity lines.
 *   UART TX ISR            : drains tx_rb into the CDC FIFO. Nothing else.
 *
 * The ring buffer has ONE producer at a time by construction: the trigger
 * thread and main are both producers, so writes are serialised with a mutex.
 * The ISR is the sole consumer.
 */

#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/ring_buffer.h>

/* --- configuration -------------------------------------------------------- */

#define IMU_ODR_HZ    208
#define ACCEL_FS_G    16
#define GYRO_FS_DPS   2000

/*
 * 208 Hz x ~48 bytes/line is roughly 10 KB/s. 8 KB of ring buffer therefore
 * absorbs about 800 ms of host stall before anything is lost — and if it ever
 * is, the seq counter makes it visible rather than silent.
 */
#define TX_BUF_BYTES  8192
#define LINE_MAX      64

/* --- LSM6DS3TR-C registers (datasheet DS12742 / AN5130) ------------------- */
/*
 * Only the pedometer block is touched here. The Zephyr lsm6dsl driver writes
 * CTRL1_XL, CTRL2_G, CTRL3_C, CTRL6_C, CTRL7_G and FIFO_CTRL5 — verified by
 * inspection — and never CTRL10_C or the step counter, so these are ours alone.
 */
#define REG_CTRL10_C        0x19U
#define CTRL10_C_FUNC_EN    BIT(2)
#define CTRL10_C_PEDO_EN    BIT(4)
#define CTRL10_C_PEDO_RST   BIT(1)

#define REG_STEP_COUNTER_L  0x4BU

static const struct device *const imu = DEVICE_DT_GET(DT_NODELABEL(lsm6ds3tr_c));
static const struct device *const cdc = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
static const struct i2c_dt_spec imu_i2c = I2C_DT_SPEC_GET(DT_NODELABEL(lsm6ds3tr_c));
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);

static uint8_t tx_storage[TX_BUF_BYTES];
static struct ring_buf tx_rb;
static K_MUTEX_DEFINE(tx_lock);

static uint32_t seq;
static uint32_t dropped;

/* --- UART TX -------------------------------------------------------------- */

static void uart_isr(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
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

		if (uart_irq_rx_ready(dev)) {
			uint8_t scratch[16];

			/* Nothing to receive; drain so the FIFO cannot wedge. */
			(void)uart_fifo_read(dev, scratch, sizeof(scratch));
		}
	}
}

/*
 * Emit a whole line or none of it. A partially written line would desynchronise
 * the host parser; a dropped line is merely a counted gap the seq exposes.
 */
static void tx_line(const char *line, size_t len)
{
	k_mutex_lock(&tx_lock, K_FOREVER);

	if (ring_buf_space_get(&tx_rb) >= len) {
		(void)ring_buf_put(&tx_rb, (const uint8_t *)line, len);
		uart_irq_tx_enable(cdc);
	} else {
		dropped++;
	}

	k_mutex_unlock(&tx_lock);
}

/* --- unit conversion ------------------------------------------------------ */

/* Zephyr reports m/s^2. 1 g == 9.80665 m/s^2. */
static int32_t to_milli_g(const struct sensor_value *v)
{
	int64_t micro_ms2 = (int64_t)v->val1 * 1000000 + v->val2;

	return (int32_t)((micro_ms2 * 1000) / 9806650);
}

/* Zephyr reports rad/s. 1 rad/s == 57.29578 deg/s. */
static int32_t to_deci_dps(const struct sensor_value *v)
{
	int64_t micro_rads = (int64_t)v->val1 * 1000000 + v->val2;

	return (int32_t)((micro_rads * 573) / 1000000);
}

/* --- pedometer ------------------------------------------------------------ */

static int pedometer_enable(void)
{
	int err;

	/* Reset the count, then enable the embedded function block and the
	 * pedometer itself. Whole-byte writes only: i2c_reg_update_byte_dt is a
	 * non-atomic read-modify-write.
	 */
	err = i2c_reg_write_byte_dt(&imu_i2c, REG_CTRL10_C,
				    CTRL10_C_FUNC_EN | CTRL10_C_PEDO_EN | CTRL10_C_PEDO_RST);
	if (err != 0) {
		return err;
	}

	k_msleep(10);

	err = i2c_reg_write_byte_dt(&imu_i2c, REG_CTRL10_C,
				    CTRL10_C_FUNC_EN | CTRL10_C_PEDO_EN);
	if (err != 0) {
		return err;
	}

	/* Read back: a lost config write leaves a counter that never advances,
	 * with no other symptom.
	 */
	uint8_t v = 0U;

	err = i2c_reg_read_byte_dt(&imu_i2c, REG_CTRL10_C, &v);
	if (err != 0) {
		return err;
	}

	return (v == (CTRL10_C_FUNC_EN | CTRL10_C_PEDO_EN)) ? 0 : -EIO;
}

static int pedometer_read(uint16_t *steps)
{
	uint8_t raw[2];
	int err = i2c_burst_read_dt(&imu_i2c, REG_STEP_COUNTER_L, raw, sizeof(raw));

	if (err != 0) {
		return err;
	}

	*steps = (uint16_t)(raw[0] | ((uint16_t)raw[1] << 8));

	return 0;
}

/* --- sample path ---------------------------------------------------------- */

/*
 * Runs on the lsm6dsl driver's own thread, once per data-ready interrupt, i.e.
 * exactly IMU_ODR_HZ times a second and synchronous with the sensor rather than
 * with a software timer.
 */
static void sample_trigger(const struct device *dev,
			   const struct sensor_trigger *trig)
{
	struct sensor_value accel[3];
	struct sensor_value gyro[3];
	char line[LINE_MAX];
	int n;

	ARG_UNUSED(trig);

	if (sensor_sample_fetch(dev) != 0) {
		return;
	}
	if (sensor_channel_get(dev, SENSOR_CHAN_ACCEL_XYZ, accel) != 0) {
		return;
	}
	if (sensor_channel_get(dev, SENSOR_CHAN_GYRO_XYZ, gyro) != 0) {
		return;
	}

	n = snprintk(line, sizeof(line), "$D,%u,%d,%d,%d,%d,%d,%d\n",
		     seq++,
		     to_milli_g(&accel[0]), to_milli_g(&accel[1]), to_milli_g(&accel[2]),
		     to_deci_dps(&gyro[0]), to_deci_dps(&gyro[1]), to_deci_dps(&gyro[2]));

	if (n > 0 && n < (int)sizeof(line)) {
		tx_line(line, (size_t)n);
	}
}

/* --- main ----------------------------------------------------------------- */

int main(void)
{
	struct sensor_trigger trig = {
		.type = SENSOR_TRIG_DATA_READY,
		.chan = SENSOR_CHAN_ALL,
	};
	struct sensor_value odr = { .val1 = IMU_ODR_HZ, .val2 = 0 };
	char line[LINE_MAX];
	bool pedo_ok;
	int n;

	ring_buf_init(&tx_rb, sizeof(tx_storage), tx_storage);

	if (gpio_is_ready_dt(&led)) {
		(void)gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	}

	if (!device_is_ready(cdc) || !device_is_ready(imu)) {
		return -ENODEV;
	}

	uart_irq_callback_user_data_set(cdc, uart_isr, NULL);
	uart_irq_rx_enable(cdc);

	/* Wait for the host to open the port so the identity line is not lost. */
	while (true) {
		uint32_t dtr = 0;

		if (uart_line_ctrl_get(cdc, UART_LINE_CTRL_DTR, &dtr) != 0 || dtr != 0U) {
			break;
		}
		k_msleep(100);
	}

	if (sensor_attr_set(imu, SENSOR_CHAN_ACCEL_XYZ,
			    SENSOR_ATTR_SAMPLING_FREQUENCY, &odr) != 0) {
		return -EIO;
	}
	if (sensor_attr_set(imu, SENSOR_CHAN_GYRO_XYZ,
			    SENSOR_ATTR_SAMPLING_FREQUENCY, &odr) != 0) {
		return -EIO;
	}

	pedo_ok = (pedometer_enable() == 0);

	/* Identity, so every recording carries its own provenance. */
	n = snprintk(line, sizeof(line), "$I,%d,%d,%d,%d\n",
		     IMU_ODR_HZ, ACCEL_FS_G, GYRO_FS_DPS, pedo_ok ? 1 : 0);
	if (n > 0) {
		tx_line(line, (size_t)n);
	}

	if (sensor_trigger_set(imu, &trig, sample_trigger) != 0) {
		n = snprintk(line, sizeof(line), "$X,trigger-failed\n");
		tx_line(line, (size_t)n);
		return -EIO;
	}

	while (1) {
		uint16_t steps = 0U;

		k_sleep(K_SECONDS(1));

		if (pedo_ok && pedometer_read(&steps) == 0) {
			n = snprintk(line, sizeof(line), "$P,%u,%u\n", seq, steps);
			if (n > 0) {
				tx_line(line, (size_t)n);
			}
		}

		/* Blue heartbeat: the rig is streaming. */
		if (gpio_is_ready_dt(&led)) {
			(void)gpio_pin_toggle_dt(&led);
		}
	}

	return 0;
}
