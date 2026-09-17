/*
 * Free-fall detection layer for the LSM6DS3TR-C.
 *
 * CO-EXISTENCE WITH THE ZEPHYR lsm6dsl DRIVER
 * -------------------------------------------
 * This module writes IMU registers directly while Zephyr's lsm6dsl driver also
 * owns the device. That is safe here, and ONLY here, because:
 *
 *   1. The register sets are disjoint. The driver touches CTRL1_XL, CTRL2_G,
 *      CTRL3_C, FIFO_CTRL5 and INT1_CTRL. We touch TAP_CFG, WAKE_UP_DUR,
 *      FREE_FALL, MD1_CFG and WAKE_UP_SRC.
 *   2. CONFIG_LSM6DSL_TRIGGER_NONE is set, so lsm6dsl_trigger.c is not compiled
 *      and the driver never writes INT1_CTRL or the MD*_CFG routing registers.
 *   3. CONFIG_PM_DEVICE is NOT enabled. The driver's PM suspend handler clears
 *      both ODR fields, which would silently stop the accelerometer and
 *      therefore the hardware free-fall detector while leaving the device
 *      looking perfectly healthy.
 *
 * All three conditions must hold. The moment we enable the FIFO, PM_DEVICE, or
 * a driver trigger, this becomes a race that cannot be locked - the driver
 * takes no lock we can honour - and the driver must be dropped from the product
 * image entirely, leaving this layer as the single owner of the chip.
 *
 * Only whole-byte register writes are used. i2c_reg_update_byte_dt() is a
 * non-atomic read-modify-write across two bus transactions and is deliberately
 * avoided.
 *
 * Execution context: thread only. Every function performs blocking I2C.
 */

#include "imu_ff.h"
#include "lsm6ds3tr_reg.h"

#include <zephyr/kernel.h>

/* Shadow copy of what we wrote, so imu_ff_verify() can detect a lost write. */
static uint8_t shadow_free_fall;
static uint8_t shadow_wake_up_dur;
static uint8_t shadow_md1_cfg;
static uint8_t shadow_tap_cfg;

static int reg_write(const struct i2c_dt_spec *bus, uint8_t reg, uint8_t val)
{
	int err = i2c_reg_write_byte_dt(bus, reg, val);

	if (err != 0) {
		return err;
	}

	/*
	 * Read back immediately. A NACKed or corrupted write to a config
	 * register produces a detector that runs forever and never fires;
	 * there is no other symptom, so the check has to happen here.
	 */
	uint8_t readback;

	err = i2c_reg_read_byte_dt(bus, reg, &readback);
	if (err != 0) {
		return err;
	}

	return (readback == val) ? 0 : -EIO;
}

static int ff_write_duration(const struct i2c_dt_spec *bus, uint8_t ths_code,
			     uint8_t dur_samples)
{
	int err;

	if (ths_code > LSM6_FF_THS_MAX || dur_samples > LSM6_FF_DUR_MAX) {
		return -EINVAL;
	}

	/*
	 * FF_DUR is split: bits 4:0 into FREE_FALL[7:3], bit 5 into
	 * WAKE_UP_DUR[7]. Shift by 3, not the 4 that Zephyr's header claims.
	 */
	shadow_free_fall = (uint8_t)(((dur_samples & 0x1FU) << LSM6_FF_DUR_LO_SHIFT) |
				     (ths_code & LSM6_FF_THS_MASK));

	shadow_wake_up_dur = (dur_samples & BIT(5)) ? LSM6_WAKE_UP_DUR_FF5 : 0U;

	err = reg_write(bus, LSM6_REG_WAKE_UP_DUR, shadow_wake_up_dur);
	if (err != 0) {
		return err;
	}

	return reg_write(bus, LSM6_REG_FREE_FALL, shadow_free_fall);
}

int imu_ff_init(const struct i2c_dt_spec *bus, uint8_t ths_code, uint8_t dur_samples)
{
	int err;

	if (!device_is_ready(bus->bus)) {
		return -ENODEV;
	}

	/* Confirm we are talking to the part we think we are. */
	uint8_t who = 0U;

	err = i2c_reg_read_byte_dt(bus, LSM6_REG_WHO_AM_I, &who);
	if (err != 0) {
		return err;
	}
	if (who != LSM6_VAL_WHO_AM_I) {
		return -ENODEV;
	}

	/*
	 * Latched interrupts. Without LIR the INT1 pulse is short and a level
	 * -triggered GPIO can miss it; with LIR the line stays asserted until
	 * WAKE_UP_SRC is read, which is what lets us use the low-power SENSE
	 * path instead of a GPIOTE channel.
	 */
	shadow_tap_cfg = LSM6_TAP_CFG_INT_EN | LSM6_TAP_CFG_LIR;
	err = reg_write(bus, LSM6_REG_TAP_CFG, shadow_tap_cfg);
	if (err != 0) {
		return err;
	}

	err = ff_write_duration(bus, ths_code, dur_samples);
	if (err != 0) {
		return err;
	}

	/* Route free-fall, and only free-fall, to INT1. */
	shadow_md1_cfg = LSM6_MD1_CFG_INT1_FF;
	err = reg_write(bus, LSM6_REG_MD1_CFG, shadow_md1_cfg);
	if (err != 0) {
		return err;
	}

	/* Clear any event latched while we were configuring. */
	bool ignored;

	return imu_ff_read_clear(bus, &ignored);
}

int imu_ff_set_threshold(const struct i2c_dt_spec *bus, uint8_t ths_code)
{
	if (ths_code > LSM6_FF_THS_MAX) {
		return -EINVAL;
	}

	/* Preserve the duration bits already in the shadow. */
	uint8_t dur_lo = (shadow_free_fall & LSM6_FF_DUR_LO_MASK) >> LSM6_FF_DUR_LO_SHIFT;
	uint8_t dur = (uint8_t)(dur_lo | ((shadow_wake_up_dur & LSM6_WAKE_UP_DUR_FF5) ? BIT(5) : 0U));

	return ff_write_duration(bus, ths_code, dur);
}

int imu_ff_read_clear(const struct i2c_dt_spec *bus, bool *was_ff)
{
	uint8_t src = 0U;
	int err;

	err = i2c_reg_read_byte_dt(bus, LSM6_REG_WAKE_UP_SRC, &src);
	if (err != 0) {
		return err;
	}

	*was_ff = ((src & LSM6_WAKE_UP_SRC_FF_IA) != 0U);

	return 0;
}

int imu_ff_verify(const struct i2c_dt_spec *bus)
{
	static const struct {
		uint8_t reg;
		const uint8_t *shadow;
	} checks[] = {
		{ LSM6_REG_TAP_CFG,     &shadow_tap_cfg },
		{ LSM6_REG_FREE_FALL,   &shadow_free_fall },
		{ LSM6_REG_WAKE_UP_DUR, &shadow_wake_up_dur },
		{ LSM6_REG_MD1_CFG,     &shadow_md1_cfg },
	};

	for (size_t i = 0; i < ARRAY_SIZE(checks); i++) {
		uint8_t val = 0U;
		int err = i2c_reg_read_byte_dt(bus, checks[i].reg, &val);

		if (err != 0) {
			return err;
		}
		if (val != *checks[i].shadow) {
			return -EIO;
		}
	}

	return 0;
}
