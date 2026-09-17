/*
 * falldetect-gkl — wearable fall detection firmware.
 *
 * Current scope: the candidate-rate probe (plan step 2).
 *
 * This is the highest-value experiment in the project and it exists to answer
 * two questions that the entire architecture assumes but nobody has measured:
 *
 *   1. How often does hardware free-fall actually fire at the WRIST during
 *      ordinary daily life? The design treats stage 1 as a nearly-free gate
 *      that lets the MCU sleep. If the real rate is hundreds per day, the power
 *      budget, the journal capacity and the "cheap gate" premise are all wrong.
 *
 *   2. What fraction of real falls does it catch? Free-fall thresholds in the
 *      literature are validated at the waist. Whatever stage 1 misses is
 *      invisible to every later stage forever, so its recall is a hard ceiling
 *      on sensitivity (criterion S1).
 *
 * Deliberately minimal: no FIFO, no gyro, no journal, no BLE. Counting only.
 * A rotating threshold sweep means one flash covers all eight FF_THS settings
 * rather than eight wear sessions.
 *
 * Execution context: main thread ticks and reports; INT1 arrives on a GPIO ISR
 * which does nothing but mask the line and hand off to the system workqueue.
 *
 * Units: acceleration in milli-g. No floating point anywhere.
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/atomic.h>

#include "imu/imu_ff.h"
#include "imu/lsm6ds3tr_reg.h"

/* Collection rate from ACCEL_PLAN.md §4.2. With CONFIG_LSM6DSL_ACCEL_ODR=0 the
 * rate is runtime-selected, so it MUST be set explicitly or the sensor can stay
 * powered down.
 */
#define IMU_ODR_HZ 208

/*
 * Free-fall duration target. FF_DUR counts accelerometer ODR periods, so the
 * sample count that represents ~150 ms depends on ODR. 150 ms sits inside the
 * 100–200 ms window ACCEL_PLAN.md §5 gives for stage 1.
 */
#define FF_DUR_TARGET_MS 150
#define FF_DUR_SAMPLES   ((FF_DUR_TARGET_MS * IMU_ODR_HZ) / 1000)
#define FF_DUR_ACTUAL_MS ((FF_DUR_SAMPLES * 1000) / IMU_ODR_HZ)

/* Each threshold gets an equal slice of wall-clock so the rates are comparable. */
#define ROTATE_SECONDS (30 * 60)
#define REPORT_SECONDS (5 * 60)

/*
 * LED status. The board has one RGB package (all three channels active-low,
 * polarity handled by the devicetree flags).
 *
 *   green  calm      — magnitude sits near 1 g, the device is still
 *   blue   motion    — magnitude has departed from 1 g
 *   red    free-fall — the hardware detector fired; held so it is visible
 *
 * Classification is done on magnitude SQUARED, which avoids a square root
 * entirely: comparing mag^2 against threshold^2 is the same test. int64 because
 * three squared axes at +/-16000 mg would sit uncomfortably close to the int32
 * ceiling.
 */
#define CALM_LO_MG      880
#define CALM_HI_MG      1120

/*
 * Hold times. Both are counted in 100 ms ticks by the sample loop, which is the
 * ONLY writer of the LEDs. The work queue merely raises a request flag - having
 * two contexts drive the LEDs directly is what made red flash for a fraction of
 * a second: the work thread lit red, then the very next classifier tick
 * overwrote it before the hold was ever read.
 */
#define RED_HOLD_TICKS  30   /* 3 s after a free-fall event                  */
#define BLUE_HOLD_TICKS 10   /* 1 s after motion stops, so it stays readable */

enum led_state {
	LED_CALM,
	LED_MOTION,
	LED_FALL,
};

static const struct device *const imu = DEVICE_DT_GET(DT_NODELABEL(lsm6ds3tr_c));
static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_blue = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);
static const struct gpio_dt_spec int1 =
	GPIO_DT_SPEC_GET(DT_NODELABEL(lsm6ds3tr_c), irq_gpios);

/*
 * Independent raw-I2C handle onto the same device. I2C_DT_SPEC_GET is a
 * compile-time devicetree macro resolving to the same bus and address the
 * driver uses; it borrows nothing from the driver's state. See imu_ff.c for the
 * conditions that make this co-existence safe.
 */
static const struct i2c_dt_spec imu_i2c = I2C_DT_SPEC_GET(DT_NODELABEL(lsm6ds3tr_c));

static struct gpio_callback int1_cb;
static struct k_work int1_work;

static struct {
	uint32_t events;
	uint32_t seconds;
} stats[LSM6_FF_THS_MAX + 1];

static uint8_t cur_ths;
static uint32_t drift_faults;
static uint32_t uptime_s;

/*
 * Raised by the work queue, consumed by the sample loop. Atomic because a plain
 * flag can lose an event in the window between test and clear, and a lost
 * free-fall is exactly the thing this firmware exists to not do.
 */
static atomic_t fall_pending;
static uint8_t red_ticks;
static uint8_t blue_ticks;

/* Exactly one channel lit at a time; "1" is logical-lit, the active-low
 * inversion lives in the devicetree flags.
 */
static void led_set(enum led_state state)
{
	(void)gpio_pin_set_dt(&led_red, state == LED_FALL);
	(void)gpio_pin_set_dt(&led_green, state == LED_CALM);
	(void)gpio_pin_set_dt(&led_blue, state == LED_MOTION);
}

/* Machine-readable status cadence, for tools/ff_gui.py. */
#define STATUS_SECONDS 2

/* Live sample stream for the host visual. 10 Hz is far too slow to see a fall
 * transient - that needs the FIFO, which is a later step - but it is enough to
 * show orientation, gravity and whether the sensor is alive at all.
 */
#define TICK_MS        100
#define TICKS_PER_SEC  (1000 / TICK_MS)

/* Zephyr reports acceleration in m/s^2. 1 g == 9.80665 m/s^2. */
static int32_t to_milli_g(const struct sensor_value *v)
{
	int64_t micro_ms2 = (int64_t)v->val1 * 1000000 + v->val2;

	return (int32_t)((micro_ms2 * 1000) / 9806650);
}

/* Zephyr reports angular rate in rad/s. 1 rad/s == 57.29578 deg/s. */
static int32_t to_deci_dps(const struct sensor_value *v)
{
	int64_t micro_rads = (int64_t)v->val1 * 1000000 + v->val2;

	return (int32_t)((micro_rads * 573) / 1000000);
}

/* --- IMU bring-up --------------------------------------------------------- */

static int imu_set_odr(enum sensor_channel chan, int hz)
{
	struct sensor_value odr = { .val1 = hz, .val2 = 0 };

	return sensor_attr_set(imu, chan, SENSOR_ATTR_SAMPLING_FREQUENCY, &odr);
}

/*
 * Prove the full-scale range actually reached the chip.
 *
 * A Kconfig value only says what the driver was asked to write. If the write
 * were lost the failure mode is silent clipping at ±2 g, which is
 * indistinguishable from real data. Reading the registers back is the only
 * thing that turns that into a detectable fault.
 */
static int imu_verify_full_scale(void)
{
	uint8_t ctrl1_xl;
	uint8_t ctrl2_g;
	int err;

	err = i2c_reg_read_byte_dt(&imu_i2c, LSM6_REG_CTRL1_XL, &ctrl1_xl);
	if (err != 0) {
		printk("FAIL: CTRL1_XL read: %d\n", err);
		return err;
	}

	err = i2c_reg_read_byte_dt(&imu_i2c, LSM6_REG_CTRL2_G, &ctrl2_g);
	if (err != 0) {
		printk("FAIL: CTRL2_G read: %d\n", err);
		return err;
	}

	printk("regs : CTRL1_XL=0x%02x CTRL2_G=0x%02x\n", ctrl1_xl, ctrl2_g);

	if ((ctrl1_xl & LSM6_CTRL1_FS_XL_MASK) != LSM6_CTRL1_FS_XL_16G) {
		printk("FAIL: accel not at +/-16 g (FS_XL=%u) - samples would clip\n",
		       (ctrl1_xl & LSM6_CTRL1_FS_XL_MASK) >> 2);
		return -EIO;
	}

	if (((ctrl2_g & LSM6_CTRL2_FS_G_MASK) != LSM6_CTRL2_FS_G_2000) ||
	    ((ctrl2_g & LSM6_CTRL2_FS_125) != 0U)) {
		/* BIT() expands to unsigned long, so cast to keep %u honest. */
		printk("FAIL: gyro not at +/-2000 dps (FS_G=%u FS_125=%u)\n",
		       (unsigned int)((ctrl2_g & LSM6_CTRL2_FS_G_MASK) >> 2),
		       (unsigned int)((ctrl2_g & LSM6_CTRL2_FS_125) >> 1));
		return -EIO;
	}

	printk("range: accel +/-16 g, gyro +/-2000 dps (verified by read-back)\n");

	return 0;
}

/* --- INT1 ----------------------------------------------------------------- */

/*
 * ISR context. Interrupts are latched (LIR) and the GPIO is level-triggered, so
 * the line stays asserted until WAKE_UP_SRC is read. Mask it here or it
 * re-enters continuously; the work item clears the source and re-arms.
 */
static void int1_isr(const struct device *port, struct gpio_callback *cb,
		     gpio_port_pins_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	(void)gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_DISABLE);
	(void)k_work_submit(&int1_work);
}

static void int1_work_fn(struct k_work *work)
{
	bool was_ff = false;
	int err;

	ARG_UNUSED(work);

	err = imu_ff_read_clear(&imu_i2c, &was_ff);
	if (err == 0 && was_ff) {
		stats[cur_ths].events++;

		/* Request only. The sample loop owns the LEDs. */
		atomic_set(&fall_pending, 1);

		/* Machine-readable, emitted immediately so the host can plot
		 * event timing rather than only aggregate counts.
		 */
		printk("$E,%u,%u\n", uptime_s, cur_ths);
	}

	(void)gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_LEVEL_ACTIVE);
}

/* --- reporting ------------------------------------------------------------ */

static void report(uint32_t uptime_s)
{
	printk("\n--- candidate rate @ %uh%02um ---\n",
	       uptime_s / 3600U, (uptime_s % 3600U) / 60U);
	printk(" FF_THS  events  exposure  events/hour\n");

	for (uint8_t i = 0; i <= LSM6_FF_THS_MAX; i++) {
		uint32_t s = stats[i].seconds;
		uint32_t per_hour_x10 = (s > 0U) ? ((stats[i].events * 36000U) / s) : 0U;

		printk("   %u    %6u   %4us     %3u.%u\n",
		       i, stats[i].events, s, per_hour_x10 / 10U, per_hour_x10 % 10U);
	}

	if (drift_faults > 0U) {
		printk("WARN: %u config-drift faults - the detector was silently dead\n",
		       drift_faults);
	}
}

/*
 * Compact status line for tools/ff_gui.py.
 *   $S,<uptime_s>,<cur_ths>,<drift_faults>,e0,s0,e1,s1,...,e7,s7
 * Kept separate from report() so the human-readable view stays readable on a
 * plain terminal and the parser never has to scrape prose.
 */
static void status_emit(void)
{
	printk("$S,%u,%u,%u", uptime_s, cur_ths, drift_faults);
	for (uint8_t i = 0; i <= LSM6_FF_THS_MAX; i++) {
		printk(",%u,%u", stats[i].events, stats[i].seconds);
	}
	printk("\n");
}

/*
 * One live sample for the host visual:
 *   $A,<ax>,<ay>,<az>,<gx>,<gy>,<gz>   accel in milli-g, gyro in deci-dps
 *
 * The host computes the magnitude sqrt(ax^2+ay^2+az^2). That is the number the
 * detector actually works on, because it is orientation-independent - a watch
 * can sit at any rotation on a wrist, so no single axis means anything on its
 * own. At rest the magnitude is ~1000 mg (gravity); free-fall drives it toward
 * zero and impact drives it well above it.
 */
static void sample_emit(void)
{
	struct sensor_value accel[3];
	struct sensor_value gyro[3];

	if (sensor_sample_fetch(imu) != 0) {
		return;
	}
	if (sensor_channel_get(imu, SENSOR_CHAN_ACCEL_XYZ, accel) != 0) {
		return;
	}
	if (sensor_channel_get(imu, SENSOR_CHAN_GYRO_XYZ, gyro) != 0) {
		return;
	}

	int32_t ax = to_milli_g(&accel[0]);
	int32_t ay = to_milli_g(&accel[1]);
	int32_t az = to_milli_g(&accel[2]);

	printk("$A,%d,%d,%d,%d,%d,%d\n", ax, ay, az,
	       to_deci_dps(&gyro[0]), to_deci_dps(&gyro[1]), to_deci_dps(&gyro[2]));

	/*
	 * Single owner of the LEDs, in strict priority order:
	 *   red   free-fall, held RED_HOLD_TICKS; a fresh event restarts the hold
	 *   blue  motion, and for BLUE_HOLD_TICKS after motion stops
	 *   green at rest with nothing pending
	 */
	if (atomic_cas(&fall_pending, 1, 0)) {
		red_ticks = RED_HOLD_TICKS;
	}

	if (red_ticks > 0U) {
		red_ticks--;
		led_set(LED_FALL);
		return;
	}

	/* Compare squared magnitudes so no square root is needed. */
	int64_t mag_sq = (int64_t)ax * ax + (int64_t)ay * ay + (int64_t)az * az;
	bool calm = (mag_sq >= (int64_t)CALM_LO_MG * CALM_LO_MG) &&
		    (mag_sq <= (int64_t)CALM_HI_MG * CALM_HI_MG);

	if (!calm) {
		blue_ticks = BLUE_HOLD_TICKS;
	}

	if (blue_ticks > 0U) {
		blue_ticks--;
		led_set(LED_MOTION);
	} else {
		led_set(LED_CALM);
	}
}

/* --- main ----------------------------------------------------------------- */

int main(void)
{
	int err;

	if (!gpio_is_ready_dt(&led_red) || !gpio_is_ready_dt(&led_green) ||
	    !gpio_is_ready_dt(&led_blue)) {
		return -ENODEV;
	}
	(void)gpio_pin_configure_dt(&led_red, GPIO_OUTPUT_INACTIVE);
	(void)gpio_pin_configure_dt(&led_green, GPIO_OUTPUT_INACTIVE);
	(void)gpio_pin_configure_dt(&led_blue, GPIO_OUTPUT_INACTIVE);

	/* Let the host attach before the banner, but do not stall forever. */
	for (int i = 0; i < 50; i++) {
		uint32_t dtr = 0;

		if (uart_line_ctrl_get(DEVICE_DT_GET(DT_CHOSEN(zephyr_console)),
				       UART_LINE_CTRL_DTR, &dtr) != 0 || dtr != 0U) {
			break;
		}
		k_msleep(100);
	}

	printk("\nfalldetect-gkl - candidate-rate probe\n");

	/*
	 * device_is_ready() implies the driver's WHO_AM_I check passed, which
	 * also proves I2C and the P1.08 sensor rail.
	 */
	if (!device_is_ready(imu)) {
		printk("FAIL: %s not ready - check I2C and the P1.08 rail\n", imu->name);
		return -ENODEV;
	}
	printk("IMU  : %s ready\n", imu->name);

	err = imu_set_odr(SENSOR_CHAN_ACCEL_XYZ, IMU_ODR_HZ);
	if (err != 0) {
		printk("FAIL: accel ODR set: %d\n", err);
		return err;
	}

	/* The gyro needs its own ODR; free-fall does not use it, but the live
	 * sample stream does and a powered-down gyro returns unchanging zeros.
	 */
	err = imu_set_odr(SENSOR_CHAN_GYRO_XYZ, IMU_ODR_HZ);
	if (err != 0) {
		printk("FAIL: gyro ODR set: %d\n", err);
		return err;
	}
	printk("ODR  : %d Hz\n", IMU_ODR_HZ);

	err = imu_verify_full_scale();
	if (err != 0) {
		return err;
	}

	/* Arm hardware free-fall and route it to INT1. */
	err = imu_ff_init(&imu_i2c, 0, FF_DUR_SAMPLES);
	if (err != 0) {
		printk("FAIL: free-fall init: %d\n", err);
		return err;
	}
	printk("FF   : DUR=%d samples (~%d ms at %d Hz), THS rotates every %d min\n",
	       FF_DUR_SAMPLES, FF_DUR_ACTUAL_MS, IMU_ODR_HZ, ROTATE_SECONDS / 60);
	printk("note : FF_THS codes are raw; the mg mapping is unconfirmed\n\n");

	if (!gpio_is_ready_dt(&int1)) {
		printk("FAIL: INT1 GPIO not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&int1, GPIO_INPUT);
	if (err != 0) {
		printk("FAIL: INT1 configure: %d\n", err);
		return err;
	}

	k_work_init(&int1_work, int1_work_fn);
	gpio_init_callback(&int1_cb, int1_isr, BIT(int1.pin));

	err = gpio_add_callback(int1.port, &int1_cb);
	if (err != 0) {
		printk("FAIL: INT1 callback: %d\n", err);
		return err;
	}

	/*
	 * Level-triggered, not edge. On nRF52 an edge interrupt allocates a
	 * GPIOTE channel, which is the classic idle-current trap; a level
	 * interrupt uses the low-power SENSE path. This board sets no
	 * sense-edge-mask, so the choice is ours to make here.
	 */
	err = gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_LEVEL_ACTIVE);
	if (err != 0) {
		printk("FAIL: INT1 interrupt configure: %d\n", err);
		return err;
	}

	uint32_t tick = 0U;

	while (1) {
		k_sleep(K_MSEC(TICK_MS));
		sample_emit();

		if (++tick < TICKS_PER_SEC) {
			continue;
		}
		tick = 0U;

		uptime_s++;
		stats[cur_ths].seconds++;

		/*
		 * A lost config write leaves a device that looks healthy and
		 * detects nothing. Checking costs one I2C burst a minute.
		 */
		if ((uptime_s % 60U) == 0U) {
			if (imu_ff_verify(&imu_i2c) != 0) {
				drift_faults++;
				printk("WARN: config drift - reconfiguring\n");
				(void)imu_ff_init(&imu_i2c, cur_ths, FF_DUR_SAMPLES);
			}
		}

		if ((uptime_s % STATUS_SECONDS) == 0U) {
			status_emit();
		}

		if ((uptime_s % REPORT_SECONDS) == 0U) {
			report(uptime_s);
		}

		if ((uptime_s % ROTATE_SECONDS) == 0U) {
			cur_ths = (cur_ths + 1U) % (LSM6_FF_THS_MAX + 1U);
			if (imu_ff_set_threshold(&imu_i2c, cur_ths) != 0) {
				printk("WARN: threshold change to %u failed\n", cur_ths);
			} else {
				printk("FF_THS -> %u\n", cur_ths);
			}
		}
	}

	return 0;
}
