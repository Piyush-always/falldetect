/*
 * Phase 0 bring-up: LED blink.
 *
 * Purpose: prove the toolchain, flash path and runtime on the XIAO nRF52840
 * Sense before any peripheral work begins. Nothing here is part of the fall
 * detector; it exists so that a later failure can be attributed to the code
 * under test rather than to the build environment.
 *
 * Execution context: LED loop runs on main thread only. BLE OTA (added on top
 * of the original blink-only scope) brings in the Bluetooth host's own
 * context for connected()/bt_ready() - both only ever call k_work_submit(),
 * never touch the LEDs or block, so the two contexts never share state.
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
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/mgmt/mcumgr/transport/smp_bt.h>
#include <zephyr/dfu/mcuboot.h>

#define BLINK_PERIOD_MS 500U

/*
 * BLE OTA advertising. Enabling CONFIG_BT_PERIPHERAL and the MCUmgr BT
 * transport (apps/blink/prj.conf) only compiles the SMP GATT service in -
 * nothing calls bt_enable()/bt_le_adv_start() on its own, so without this the
 * radio never turns on. Pattern matches Zephyr's own reference
 * (samples/subsys/mgmt/mcumgr/smp_svr/src/bluetooth.c) rather than
 * hand-rolled advertising params.
 *
 * Execution context: bt_ready()/connected()/disconnected() run on the
 * Bluetooth host's own context, not main thread; advertise() is deferred onto
 * the system workqueue via k_work so nothing blocking happens in those
 * callbacks.
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

static void connected(struct bt_conn *conn, uint8_t err)
{
	ARG_UNUSED(conn);

	if (err != 0) {
		k_work_submit(&advertise_work);
	}
}

static void on_conn_recycled(void)
{
	k_work_submit(&advertise_work);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.recycled = on_conn_recycled,
};

static void bt_ready(int err)
{
	if (err == 0) {
		k_work_submit(&advertise_work);
	}
}

static const struct gpio_dt_spec leds[] = {
	GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios), /* Red   */
	GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios), /* Green */
	GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios), /* Blue  */
};

int main(void)
{
	int err;
	bool lit = false;

	k_work_init(&advertise_work, advertise);
	err = bt_enable(bt_ready);
	if (err != 0) {
		return err;
	}

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

	/*
	 * MCUboot reverts to the previous image on the NEXT reset unless the
	 * running one explicitly confirms itself - a forgotten manual confirm
	 * from the OTA client would otherwise silently undo every update.
	 * Gated behind the LED init above passing, not called unconditionally
	 * at the top: a build that cannot even do that stays reachable over
	 * BLE to receive a fix, but still reverts if power-cycled meanwhile.
	 */
	if (!boot_is_img_confirmed()) {
		(void)boot_write_img_confirmed();
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
