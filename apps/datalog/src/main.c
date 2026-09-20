/*
 * falldetect-gkl — labelled data-collection rig.
 *
 * Streams every IMU sample over USB CDC so tools/bench/datalog_gui.py can record
 * labelled activity sessions. This corpus is the input to everything
 * downstream: the activity classifier, the fall thresholds, and the validation
 * of both. Nothing here detects anything — it only captures, faithfully.
 *
 * Wire format, one line per sample:
 *     $D,<seq>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
 *     $P,<seq>,<steps>          hardware step counter, 1 Hz
 *     $B,<seq>,<count>          short press - recording toggle
 *     $C,<seq>                  long press - alarm cancelled by wearer
 * Host -> device (NUS RX): 'A' pre-alert, 'F' alarm, 'C' clear
 *     $V,<pct>,<mv>,<charging>  battery, 1 Hz (mv is raw - see overlay)
 *     $S,<subscribed>,<usb_drops>,<ble_drops>   status, 1 Hz
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
 *   Bluetooth host context  : connected()/bt_ready(), only ever call
 *                             k_work_submit() - never touch tx_rb or the IMU.
 *
 * The ring buffer has ONE producer at a time by construction: the trigger
 * thread and main are both producers, so writes are serialised with a mutex.
 * The ISR is the sole consumer.
 *
 * BLE OTA is wired in (sysbuild.conf / boards/xiao_ble_nrf52840_sense.overlay)
 * so updates ship wirelessly instead of only over USB - same fragment and
 * pattern as apps/blink, where it was proved out first. It shares no state
 * with the CDC sample path; the two are isolated on purpose.
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
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/mgmt/mcumgr/transport/smp_bt.h>
#include <zephyr/dfu/mcuboot.h>
#include <bluetooth/services/nus.h>
#include <zephyr/bluetooth/services/bas.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/adc/voltage_divider.h>

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
/*
 * All three channels of the one RGB package (active-low; the devicetree flags
 * carry the inversion, so "1" means lit here). Driving all three together
 * reads as white - with a blue/violet cast, since the dies have different
 * forward voltages behind fixed resistors. Balancing that needs PWM.
 */
static const struct gpio_dt_spec led_r = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_g = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_b = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);

/* Cancel button: D10 / P1.15, shorted to GND when pressed. See the overlay. */
static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

static uint8_t tx_storage[TX_BUF_BYTES];
static struct ring_buf tx_rb;
static K_MUTEX_DEFINE(tx_lock);

static uint32_t seq;
static uint32_t dropped;
static uint32_t ble_dropped;

/*
 * BLE egress. Separate from the USB ring buffer on purpose: the two links
 * drain at very different rates, and a stalled BLE client must never cost a
 * USB sample.
 */
#define BLE_BUF_BYTES 4096
#define BLE_CHUNK_MAX 244      /* fits a 247-byte ATT MTU, the practical max */
#define BLE_TX_STACK  1024
#define BLE_TX_PRIO   7        /* below the sensor trigger thread (5) */
#define BLE_MAX_WAIT_MS 20U    /* cap on how long a partial chunk waits */

/*
 * Cancel button, polled in the 100 ms loop rather than driven by an edge ISR:
 * telling a short press from a long one needs the press DURATION, which an
 * edge interrupt does not carry. Polling gets it for free and removes the ISR
 * and its debounce entirely.
 *
 * LONG press stands an alarm down. Chosen over a double-tap because it needs
 * no timing precision - the wearer just holds it - and the person using this
 * may be shaken. It is still deliberate, so knocking the device against
 * furniture cannot silence a real alarm.
 */
#define BUTTON_LONG_TICKS  15   /* 1.5 s at TICK_MS */
#define BUTTON_WHITE_MS    3000

/*
 * Alert indication. The LED is NOT the wearer's alarm - a pendant sits on the
 * chest and someone lying on the floor cannot see it. This is honest feedback
 * for an observer, and a preview of the real flow, until a buzzer exists.
 * See PROJECT_OUTLINE.md section 6.
 *
 * Blink periods in 100 ms ticks. Slow = "about to alarm, you can still stop
 * it"; fast = "alarming now". The rate is the message, so they are far enough
 * apart to tell apart across a room.
 */
#define ALERT_PRE_TICKS    5    /* red, 500 ms on / 500 ms off */
#define ALERT_ALARM_TICKS  1    /* red, 100 ms on / 100 ms off */

enum alert_state {
	ALERT_IDLE = 0,   /* dark: nothing wrong, nothing to show */
	ALERT_PRE,        /* slow red: cancel window open */
	ALERT_ALARM,      /* fast red: alarm active */
};
#define TICK_MS            100   /* loop period; also the button response time */
#define TICKS_PER_SEC      (1000 / TICK_MS)

static uint8_t ble_storage[BLE_BUF_BYTES];
static struct ring_buf ble_rb;
static K_MUTEX_DEFINE(ble_lock);

/*
 * Tracked for bt_nus_send() - NUS addresses a specific connection rather
 * than broadcasting, unlike advertising. Single-connection device (BT_MAX_CONN
 * default), so one pointer is enough; cleared on disconnect (see the BLE OTA
 * block further down) so a stale handle is never sent to.
 */
static struct bt_conn *nus_conn;

/*
 * Whether a client has actually enabled notifications on the NUS TX
 * characteristic. This is NOT the same as being connected, and conflating the
 * two cost real debugging time:
 *
 * bt_nus_send() calls bt_gatt_is_subscribed() and returns -EINVAL if the peer
 * has not written the CCC descriptor. Windows CACHES that descriptor for
 * paired devices - so after the device reboots (a firmware update clears its
 * CCC state) Windows believes the subscription is still live and never
 * re-writes it. Result: connected, start_notify() reports success, and the
 * device refuses to send a single byte.
 *
 * Tracking it explicitly means the condition is reportable instead of
 * presenting as "connected but silent".
 */
static atomic_t nus_subscribed;

/*
 * The LED has exactly ONE writer: the sample loop. Nothing else touches it.
 * Letting two contexts drive the LEDs directly is what made the status LED
 * flash for a fraction of a second in src/main.c; do not repeat it here.
 */
static uint32_t button_count;

/*
 * Set from the NUS RX callback (Bluetooth host context), read by the sample
 * loop. The HOST owns detection and the countdown; the device owns the
 * physical indication and the cancel input. Keeping the split that way means
 * thresholds stay tunable on the PC without a reflash.
 *
 * atomic because the two run on different contexts and a lost alarm command
 * is exactly the failure this device exists to avoid.
 */
static atomic_t alert_state = ATOMIC_INIT(ALERT_IDLE);

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

/*
 * Wireless mirror of tx_line(). Produces into a ring buffer; ble_tx_thread()
 * below drains it in MTU-sized chunks.
 *
 * WHY NOT SEND THE LINE DIRECTLY (the bug this replaces): bt_nus_send() fails
 * outright if len exceeds the negotiated ATT MTU minus 3. The default MTU is
 * 23, so the usable payload is 20 bytes - SMALLER THAN ONE $D LINE. Every
 * sample was therefore rejected and only the 13-byte $P pedometer line got
 * through. Measured over BLE: 1 notification/sec instead of 208, carrying no
 * sample data at all, while USB was perfectly healthy.
 *
 * Chunking by MTU rather than by line is safe because the host reassembles on
 * newlines, not on notification boundaries - see LinkBase.feed_bytes().
 */
static void ble_tx_line(const char *line, size_t len)
{
	/* Subscription, not connection: a connected client that never wrote the
	 * CCC descriptor cannot receive notifications, and queueing for it just
	 * fills the buffer and inflates the drop count.
	 */
	if (atomic_get(&nus_subscribed) == 0) {
		return;
	}

	k_mutex_lock(&ble_lock, K_FOREVER);
	if (ring_buf_space_get(&ble_rb) >= len) {
		(void)ring_buf_put(&ble_rb, (const uint8_t *)line, len);
	} else {
		/* Link cannot keep up. Drop a whole line, never half of one:
		 * a torn line desynchronises the host parser, whereas a
		 * dropped one is a seq gap the host already reports.
		 */
		ble_dropped++;
	}
	k_mutex_unlock(&ble_lock);
}

/*
 * Execution context: its own cooperative thread, NOT the sensor trigger.
 * bt_nus_send() can fail with -ENOMEM when the ATT buffer pool is momentarily
 * full, and the right response is to wait and re-send the SAME bytes - which
 * means blocking. Doing that on the trigger thread would stall sampling and
 * corrupt the USB stream too.
 */
static void ble_tx_thread(void *a, void *b, void *c)
{
	uint8_t chunk[BLE_CHUNK_MAX];
	uint32_t waited_ms = 0U;

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	while (1) {
		if (atomic_get(&nus_subscribed) == 0 || nus_conn == NULL) {
			k_msleep(100);
			continue;
		}

		/* Ask every iteration: the MTU is renegotiated after connect,
		 * so a value cached at connect time would be the useless 23.
		 */
		uint32_t mtu = bt_nus_get_mtu(nus_conn);
		uint32_t want = MIN(mtu, sizeof(chunk));

		/*
		 * Batch before sending. Draining the instant a byte appears
		 * produced 207 notifications/sec of ~30 bytes each - only
		 * 6 KB/s against the ~9.6 KB/s the 208 Hz stream generates, so
		 * the buffer overflowed and a third of all samples were lost.
		 * The link was never the limit: at 495-byte payloads the same
		 * 207 notifications/sec carries far more than we need. Waiting
		 * for a full chunk is what turns notification RATE into
		 * throughput.
		 */
		k_mutex_lock(&ble_lock, K_FOREVER);
		uint32_t avail = ring_buf_size_get(&ble_rb);
		k_mutex_unlock(&ble_lock);

		if (avail == 0U) {
			waited_ms = 0U;
			k_msleep(2);
			continue;
		}

		/* ...but never sit on data indefinitely: the 1 Hz $P line and
		 * the one-shot $I would otherwise be held back behind a
		 * half-empty buffer, and the host would look stalled.
		 */
		if (avail < want && waited_ms < BLE_MAX_WAIT_MS) {
			waited_ms += 2U;
			k_msleep(2);
			continue;
		}
		waited_ms = 0U;

		k_mutex_lock(&ble_lock, K_FOREVER);
		uint32_t n = ring_buf_get(&ble_rb, chunk, want);
		k_mutex_unlock(&ble_lock);

		if (n == 0U) {
			k_msleep(2);
			continue;
		}

		int err = bt_nus_send(nus_conn, chunk, (uint16_t)n);

		if (err == -ENOMEM || err == -EAGAIN) {
			/* Buffers full: let them drain, then resend these
			 * bytes. Putting them back preserves stream order.
			 */
			k_mutex_lock(&ble_lock, K_FOREVER);
			(void)ring_buf_put(&ble_rb, chunk, n);
			k_mutex_unlock(&ble_lock);
			k_msleep(4);
		} else if (err != 0) {
			ble_dropped++;
		}
	}
}

K_THREAD_DEFINE(ble_tx_tid, BLE_TX_STACK, ble_tx_thread, NULL, NULL, NULL,
		BLE_TX_PRIO, 0, 0);

/* --- alert commands from the host ----------------------------------------- */

/*
 * Single-byte commands written to the NUS RX characteristic:
 *
 *   'A'  pre-alert  - a fall is suspected, the cancel window is open
 *   'F'  alarm      - the window expired, this is now a real alert
 *   'C'  clear      - stand down, back to normal
 *
 * Deliberately one byte: this path exists so the HOST can drive the
 * indication, not as a general command channel. Anything richer belongs in a
 * proper GATT service with its own characteristics.
 *
 * Execution context: Bluetooth host context. Sets an atomic and returns -
 * no LED writes here, because the sample loop is the only LED writer.
 */
static void nus_received(struct bt_conn *conn, const uint8_t *const data,
			 uint16_t len)
{
	ARG_UNUSED(conn);

	for (uint16_t i = 0; i < len; i++) {
		switch (data[i]) {
		case 'A':
			atomic_set(&alert_state, ALERT_PRE);
			break;
		case 'F':
			atomic_set(&alert_state, ALERT_ALARM);
			break;
		case 'C':
			atomic_set(&alert_state, ALERT_IDLE);
			break;
		default:
			/* Unknown byte: ignored rather than guessed at. */
			break;
		}
	}
}

static void nus_send_enabled(enum bt_nus_send_status status)
{
	atomic_set(&nus_subscribed, status == BT_NUS_SEND_STATUS_ENABLED);
}

static struct bt_nus_cb nus_callbacks = {
	.received = nus_received,
	.send_enabled = nus_send_enabled,
};

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


/* --- battery -------------------------------------------------------------- */

/*
 * Voltage divider on P0.31/AIN7, gated by P0.14. See the overlay: those values
 * are ASSUMED from community sources, not from the Seeed datasheet, so the raw
 * millivolt figure is reported alongside the percentage - a wrong ratio shows
 * up immediately as an implausible voltage rather than a plausible-looking but
 * wrong percentage.
 *
 * Execution context: main thread only. adc_read() blocks.
 */
static const struct voltage_divider_dt_spec vbatt =
	VOLTAGE_DIVIDER_DT_SPEC_GET(DT_PATH(vbatt));
static const struct gpio_dt_spec charging =
	GPIO_DT_SPEC_GET(DT_NODELABEL(charge_status), gpios);

/*
 * Li-Po discharge curve, flattened to a straight line between these two.
 * Deliberately crude: a real curve is non-linear and load-dependent, and
 * pretending otherwise would imply a precision this measurement does not have.
 * What matters for a worn safety device is "still fine" vs "charge me now".
 */
#define BATT_FULL_MV  4200
#define BATT_EMPTY_MV 3300

/*
 * The divider's enable line. voltage_divider.h provides ONLY the scaling
 * helper - there is no setup function - so power-gpios has to be driven here.
 * It is held off except while sampling: leaving a 1.5 Mohm divider across the
 * cell drains it continuously, which is the opposite of what a device meant to
 * run for days should do.
 */
static const struct gpio_dt_spec vbatt_power =
	GPIO_DT_SPEC_GET(DT_PATH(vbatt), power_gpios);

static bool vbatt_ready;

static int battery_mv(void)
{
	int32_t sample = 0;
	struct adc_sequence seq;
	int32_t val_mv;
	int err;

	if (!adc_is_ready_dt(&vbatt.port) || !gpio_is_ready_dt(&vbatt_power)) {
		return -ENODEV;
	}

	/* One-time channel setup. Cheap to guard, and doing it per read would
	 * reconfigure the ADC 1 Hz forever for no reason.
	 */
	if (!vbatt_ready) {
		err = adc_channel_setup_dt(&vbatt.port);
		if (err != 0) {
			return err;
		}
		err = gpio_pin_configure_dt(&vbatt_power, GPIO_OUTPUT_INACTIVE);
		if (err != 0) {
			return err;
		}
		vbatt_ready = true;
	}

	(void)gpio_pin_set_dt(&vbatt_power, 1);
	/* Let the divider settle before sampling; it is an RC with the ADC's
	 * input capacitance, and reading immediately gives a low value.
	 */
	k_msleep(1);

	err = adc_sequence_init_dt(&vbatt.port, &seq);
	if (err != 0) {
		(void)gpio_pin_set_dt(&vbatt_power, 0);
		return err;
	}
	seq.buffer = &sample;
	seq.buffer_size = sizeof(sample);

	err = adc_read(vbatt.port.dev, &seq);

	/* Off again regardless of the outcome - a failed read must not leave
	 * the divider draining the battery.
	 */
	(void)gpio_pin_set_dt(&vbatt_power, 0);

	if (err != 0) {
		return err;
	}

	val_mv = sample;
	err = adc_raw_to_millivolts_dt(&vbatt.port, &val_mv);
	if (err != 0) {
		return err;
	}

	/* Undo the divider to get the cell voltage. */
	err = voltage_divider_scale_dt(&vbatt, &val_mv);
	if (err != 0) {
		return err;
	}

	return val_mv;
}

static uint8_t battery_percent(int mv)
{
	if (mv >= BATT_FULL_MV) {
		return 100U;
	}
	if (mv <= BATT_EMPTY_MV) {
		return 0U;
	}
	return (uint8_t)(((mv - BATT_EMPTY_MV) * 100) /
			 (BATT_FULL_MV - BATT_EMPTY_MV));
}

static bool battery_charging(void)
{
	if (!gpio_is_ready_dt(&charging)) {
		return false;
	}
	/* Active-low via the devicetree flags, so 1 means "charging". */
	return gpio_pin_get_dt(&charging) == 1;
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
		ble_tx_line(line, (size_t)n);
	}
}

/* --- BLE OTA ---------------------------------------------------------------
 *
 * Enabling CONFIG_BT_PERIPHERAL and the MCUmgr BT transport (prj.conf) only
 * compiles the SMP GATT service in - nothing calls bt_enable()/
 * bt_le_adv_start() on its own. Pattern matches Zephyr's own reference
 * (samples/subsys/mgmt/mcumgr/smp_svr/src/bluetooth.c), proved out first in
 * apps/blink. Shares no state with the CDC sample path (tx_rb, uart_isr) -
 * BLE is a completely separate transport.
 *
 * Execution context: advertise()/connected()/bt_ready() run on the Bluetooth
 * host's own context, not main thread; advertise() is deferred onto the
 * system workqueue via k_work so nothing blocking happens in those callbacks.
 */
static struct k_work advertise_work;

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, SMP_BT_SVC_UUID_VAL),
};

static const struct bt_data sd[] = {
	BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME, sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static void advertise(struct k_work *work)
{
	ARG_UNUSED(work);

	(void)bt_le_adv_start(BT_LE_ADV_CONN_FAST_1, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
}

static void mtu_exchanged(struct bt_conn *conn, uint8_t err,
			  struct bt_gatt_exchange_params *params)
{
	ARG_UNUSED(params);

	if (err == 0) {
		/* Now large enough for whole lines; the tx thread picks the
		 * new size up on its next iteration.
		 */
		(void)bt_gatt_get_mtu(conn);
	}
}

static struct bt_gatt_exchange_params mtu_params = { .func = mtu_exchanged };

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err != 0) {
		k_work_submit(&advertise_work);
		return;
	}
	nus_conn = conn;

	/*
	 * Request a bigger ATT MTU ourselves. This is normally the central's
	 * job, but Windows does not do it here, and the 23-byte default leaves
	 * a 20-byte payload - too small for a single sample line. Without this
	 * the telemetry stream carries nothing.
	 */
	(void)bt_gatt_exchange_mtu(conn, &mtu_params);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(reason);

	nus_conn = NULL;
}

static void on_conn_recycled(void)
{
	k_work_submit(&advertise_work);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.recycled = on_conn_recycled,
};

static void bt_ready(int err)
{
	if (err == 0) {
		k_work_submit(&advertise_work);
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

	/*
	 * Enabled first and unconditionally: if IMU/CDC bring-up below fails,
	 * this rig should still be reachable over BLE to receive a fixed
	 * build rather than being stranded until the next physical USB
	 * recovery.
	 */
	k_work_init(&advertise_work, advertise);
	/*
	 * Return code intentionally not surfaced: no output channel exists
	 * yet at this point (tx_rb below, CDC further down), and this rig's
	 * primary job - the USB CDC sample stream - does not depend on BLE
	 * succeeding either way.
	 */
	(void)bt_enable(bt_ready);
	/*
	 * The RX callback is how the host drives the alert indication ('A'
	 * pre-alert, 'F' alarm, 'C' clear). NUS's GATT service is registered
	 * statically at link time (BT_GATT_SERVICE_DEFINE in nus.c); this call
	 * only wires up callbacks, so it need not wait for bt_ready().
	 */
	(void)bt_nus_init(&nus_callbacks);

	ring_buf_init(&tx_rb, sizeof(tx_storage), tx_storage);
	ring_buf_init(&ble_rb, sizeof(ble_storage), ble_storage);

	if (gpio_is_ready_dt(&led_r) && gpio_is_ready_dt(&led_g) &&
	    gpio_is_ready_dt(&led_b)) {
		(void)gpio_pin_configure_dt(&led_r, GPIO_OUTPUT_INACTIVE);
		(void)gpio_pin_configure_dt(&led_g, GPIO_OUTPUT_INACTIVE);
		(void)gpio_pin_configure_dt(&led_b, GPIO_OUTPUT_INACTIVE);
	}

	/*
	 * Cancel button. Input only - it is polled in the sample loop, because
	 * telling a short press from a long one needs the press duration and an
	 * edge interrupt does not carry it.
	 */
	if (gpio_is_ready_dt(&button)) {
		(void)gpio_pin_configure_dt(&button, GPIO_INPUT);
	}

	if (!device_is_ready(cdc) || !device_is_ready(imu)) {
		return -ENODEV;
	}

	uart_irq_callback_user_data_set(cdc, uart_isr, NULL);
	uart_irq_rx_enable(cdc);

	/*
	 * Wait for the host to open the port so the identity line is not lost -
	 * bounded, not indefinite. An unbounded wait here means main() never
	 * reaches sensor_trigger_set() or the BLE self-confirm below it when
	 * nothing has the COM port open, which silently defeats OTA: MCUboot
	 * reverts an unconfirmed test image on the next reset regardless of
	 * whether BLE itself is up. Same bound as apps/blink and src/main.c.
	 */
	for (int i = 0; i < 50; i++) {
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
		ble_tx_line(line, (size_t)n);
	}

	if (sensor_trigger_set(imu, &trig, sample_trigger) != 0) {
		n = snprintk(line, sizeof(line), "$X,trigger-failed\n");
		tx_line(line, (size_t)n);
		ble_tx_line(line, (size_t)n);
		return -EIO;
	}

	/*
	 * MCUboot reverts to the previous image on the NEXT reset unless the
	 * running one explicitly confirms itself - a forgotten manual confirm
	 * from the OTA client would otherwise silently undo every update.
	 * Gated behind CDC + IMU + trigger bring-up above all succeeding, not
	 * called unconditionally at the top: a build that cannot even do that
	 * stays reachable over BLE (enabled earlier, independent of IMU/CDC
	 * state) to receive a fix, but still reverts if power-cycled meanwhile.
	 */
	if (!boot_is_img_confirmed()) {
		(void)boot_write_img_confirmed();
	}

	uint32_t tick = 0U;
	uint32_t white_ticks = 0U;
	uint32_t held_ticks = 0U;

	/*
	 * 100 ms rather than 500 ms: this loop is the ONLY writer of the LEDs,
	 * so its period is also the button's response time, and half a second
	 * to acknowledge a press feels broken. The pedometer poll is
	 * decimated back to 1 Hz so fd_studio's step-rate calculation, which
	 * assumes ~1 Hz $P lines, is unaffected.
	 */
	while (1) {
		uint16_t steps = 0U;

		k_sleep(K_MSEC(TICK_MS));
		tick++;

		/* --- button: polled, so press DURATION is available --------- */
		{
			int alert = (int)atomic_get(&alert_state);
			bool down = gpio_is_ready_dt(&button) &&
				    gpio_pin_get_dt(&button) == 1;

			if (down) {
				held_ticks++;
				/* Acknowledge the moment the long press is
				 * reached rather than on release - holding a
				 * button with no feedback feels broken, and a
				 * frightened person will let go early.
				 */
				if (held_ticks == BUTTON_LONG_TICKS &&
				    alert != ALERT_IDLE) {
					atomic_set(&alert_state, ALERT_IDLE);
					white_ticks = BUTTON_WHITE_MS / TICK_MS;
					n = snprintk(line, sizeof(line),
						     "$C,%u\n", seq);
					if (n > 0) {
						tx_line(line, (size_t)n);
						ble_tx_line(line, (size_t)n);
					}
				}
			} else if (held_ticks > 0U) {
				/* Released. A short press is the recording
				 * toggle; during an alert it does nothing, so
				 * a knock cannot stand a real alarm down.
				 */
				if (held_ticks < BUTTON_LONG_TICKS &&
				    alert == ALERT_IDLE) {
					white_ticks = BUTTON_WHITE_MS / TICK_MS;
					button_count++;
					n = snprintk(line, sizeof(line),
						     "$B,%u,%u\n", seq, button_count);
					if (n > 0) {
						tx_line(line, (size_t)n);
						ble_tx_line(line, (size_t)n);
					}
				}
				held_ticks = 0U;
			}
		}

		/* --- LEDs: single owner, strict priority -------------------- */
		{
			int alert = (int)atomic_get(&alert_state);

			if (alert != ALERT_IDLE) {
				/* Alert outranks everything, including the
				 * cancel acknowledgement - the alarm must not
				 * be maskable by an unrelated press.
				 */
				uint32_t period = (alert == ALERT_ALARM)
						  ? ALERT_ALARM_TICKS
						  : ALERT_PRE_TICKS;
				bool on = ((tick / period) % 2U) == 0U;

				(void)gpio_pin_set_dt(&led_r, on);
				(void)gpio_pin_set_dt(&led_g, 0);
				(void)gpio_pin_set_dt(&led_b, 0);
			} else if (white_ticks > 0U) {
				white_ticks--;
				/* White = all three channels: press understood. */
				(void)gpio_pin_set_dt(&led_r, 1);
				(void)gpio_pin_set_dt(&led_g, 1);
				(void)gpio_pin_set_dt(&led_b, 1);
			} else {
				/*
				 * Idle is DARK, not a heartbeat. A pendant worn
				 * on the chest all day should not strobe, and an
				 * always-on indicator is the thing a wearer
				 * stops noticing - so it cannot carry meaning.
				 * Dark also makes the short press worth
				 * something: tap it and white for 3 s answers
				 * "is this thing alive?".
				 *
				 * Costs a little: you cannot tell "streaming"
				 * from "powered but idle" by looking. The host
				 * shows that, and the wearer does not care.
				 */
				(void)gpio_pin_set_dt(&led_r, 0);
				(void)gpio_pin_set_dt(&led_g, 0);
				(void)gpio_pin_set_dt(&led_b, 0);
			}
		}

		/* --- pedometer, once per second ----------------------------- */
		if ((tick % TICKS_PER_SEC) != 0U) {
			continue;
		}

		/* --- battery, 1 Hz ----------------------------------------- */
		{
			int mv = battery_mv();

			if (mv > 0) {
				uint8_t pct = battery_percent(mv);
				bool chg = battery_charging();

				/* Standard Battery Service, so any BLE tool sees it. */
				(void)bt_bas_set_battery_level(pct);
				bt_bas_bls_set_battery_charge_state(
					chg ? BT_BAS_BLS_CHARGE_STATE_CHARGING
					    : BT_BAS_BLS_CHARGE_STATE_DISCHARGING_ACTIVE);

				/* Raw mV is deliberately included: the divider values
				 * are ASSUMED, and an implausible voltage is the only
				 * way to notice a wrong ratio before trusting a
				 * plausible-looking percentage.
				 */
				n = snprintk(line, sizeof(line), "$V,%u,%d,%u\n",
					     pct, mv, chg ? 1U : 0U);
				if (n > 0) {
					tx_line(line, (size_t)n);
					ble_tx_line(line, (size_t)n);
				}
			}
		}

		/*
		 * Status, 1 Hz. Goes out over USB CDC ALWAYS - including when
		 * no BLE client is subscribed, which is precisely the case it
		 * exists to explain. "sub=0" with a client connected means the
		 * peer never wrote the CCC descriptor (Windows caches it for
		 * paired devices), and that is why no samples are arriving.
		 */
		n = snprintk(line, sizeof(line), "$S,%u,%u,%u\n",
			     (unsigned int)atomic_get(&nus_subscribed),
			     dropped, ble_dropped);
		if (n > 0) {
			tx_line(line, (size_t)n);
			ble_tx_line(line, (size_t)n);
		}

		if (pedo_ok && pedometer_read(&steps) == 0) {
			n = snprintk(line, sizeof(line), "$P,%u,%u\n", seq, steps);
			if (n > 0) {
				tx_line(line, (size_t)n);
				ble_tx_line(line, (size_t)n);
			}
		}
	}

	return 0;
}
