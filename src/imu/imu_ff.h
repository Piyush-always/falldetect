/*
 * Free-fall detection layer for the LSM6DS3TR-C.
 *
 * Raw register access, layered on top of Zephyr's lsm6dsl driver rather than
 * replacing it (yet). That co-existence is safe ONLY under the conditions
 * documented in imu_ff.c - read them before enabling PM_DEVICE or the FIFO.
 *
 * Execution context: all functions are blocking I2C calls and must be called
 * from a thread, never from an ISR.
 */

#ifndef IMU_FF_H_
#define IMU_FF_H_

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/drivers/i2c.h>

/**
 * Arm hardware free-fall detection and route it to INT1.
 *
 * Uses latched interrupt mode (LIR), so INT1 stays asserted until
 * imu_ff_read_clear() is called. That allows a level-triggered GPIO interrupt,
 * which on nRF52 uses the low-power SENSE path instead of allocating a GPIOTE
 * channel.
 *
 * @param bus       I2C spec for the IMU.
 * @param ths_code  FF_THS code, 0..LSM6_FF_THS_MAX. Higher is a larger
 *                  threshold, i.e. easier to trigger.
 * @param dur_samples FF_DUR in accelerometer ODR periods, 0..LSM6_FF_DUR_MAX.
 * @return 0 on success, negative errno otherwise.
 */
int imu_ff_init(const struct i2c_dt_spec *bus, uint8_t ths_code, uint8_t dur_samples);

/**
 * Change the free-fall threshold without touching duration or routing.
 * Used to sweep thresholds during a single wear session.
 */
int imu_ff_set_threshold(const struct i2c_dt_spec *bus, uint8_t ths_code);

/**
 * Read and clear the latched interrupt source.
 *
 * @param bus    I2C spec for the IMU.
 * @param was_ff Set true if the event was a free-fall.
 * @return 0 on success, negative errno otherwise.
 */
int imu_ff_read_clear(const struct i2c_dt_spec *bus, bool *was_ff);

/**
 * Read back the registers this module configured and compare against what was
 * written. Detects silent config drift - a lost write leaves the device looking
 * healthy while detecting nothing.
 *
 * @return 0 if configuration matches, -EIO on mismatch, negative errno on
 *         bus failure.
 */
int imu_ff_verify(const struct i2c_dt_spec *bus);

#endif /* IMU_FF_H_ */
