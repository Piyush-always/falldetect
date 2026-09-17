/*
 * Phase 0 bring-up: LED blink.
 *
 * Purpose: prove the toolchain, flash path and runtime on the XIAO nRF52840
 * Sense before any peripheral work begins. Nothing here is part of the fall
 * detector; it exists so that a later failure can be attributed to the code
 * under test rather than to the build environment.
 *
 * Execution context: main thread only. No ISRs, no shared state, no sleep-mode
 * handling yet.
 *
 * Board facts (Verified against
 * zephyr/boards/seeed/xiao_ble/xiao_ble_common.dtsi):
 *   led0 = Red   P0.26, active-low
 *   led1 = Green P0.30, active-low
 *   led2 = Blue  P0.06, active-low
 *
 * The active-low polarity is carried by the devicetree GPIO flags, so
 * gpio_pin_set_dt()/gpio_pin_toggle_dt() take logical levels here: "1" means
 * lit. Do not open-code the inversion.
 *
 * All three channels sit in one RGB package, so driving them together reads as
 * white. It will not be a neutral white: the three dies have different forward
 * voltages behind fixed series resistors, so expect a blue/violet cast.
 * Balancing the channels needs PWM duty control, not plain GPIO.
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>

#define BLINK_PERIOD_MS 500U

static const struct gpio_dt_spec leds[] = {
	GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios), /* Red   */
	GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios), /* Green */
	GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios), /* Blue  */
};

int main(void)
{
	int err;
	bool lit = false;

	for (size_t i = 0; i < ARRAY_SIZE(leds); i++) {
		if (!gpio_is_ready_dt(&leds[i])) {
			/* GPIO controller not ready: nothing further is meaningful. */
			return -ENODEV;
		}

		/* Start dark, so the first transition is visibly a change. */
		err = gpio_pin_configure_dt(&leds[i], GPIO_OUTPUT_INACTIVE);
		if (err != 0) {
			return err;
		}
	}

	while (1) {
		lit = !lit;

		/*
		 * Drive all three from one state variable rather than toggling
		 * each independently: the channels then cannot drift out of
		 * step, which would show up as a colour tint instead of white.
		 */
		for (size_t i = 0; i < ARRAY_SIZE(leds); i++) {
			err = gpio_pin_set_dt(&leds[i], lit);
			if (err != 0) {
				return err;
			}
		}

		k_msleep(BLINK_PERIOD_MS);
	}

	return 0;
}
