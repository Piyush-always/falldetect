/*
 * LSM6DS3TR-C register definitions.
 *
 * Transcribed deliberately rather than reusing Zephyr's private
 * drivers/sensor/st/lsm6dsl/lsm6dsl.h, because that header contains a defect:
 *
 *     #define LSM6DSL_MASK_FREE_FALL_DUR   (BIT(7)|BIT(6)|BIT(5)|BIT(4)|BIT(3))
 *     #define LSM6DSL_SHIFT_FREE_FALL_DUR  4
 *
 * The mask covers bits 7:3, so the shift must be 3. Using 4 halves every
 * free-fall duration written to the chip - a silent sensitivity change on a
 * safety device, which is exactly the class of bug ACCEL_PLAN.md §2 requires us
 * to avoid by owning this header.
 *
 * Every field below was cross-checked for internal consistency (the lowest set
 * bit of each mask equals its shift). Fields marked ASSUMED are labels only -
 * no code behaviour depends on them - and must be confirmed against the ST
 * datasheet (DS12742) / AN5130 before they are quoted as fact.
 */

#ifndef LSM6DS3TR_REG_H_
#define LSM6DS3TR_REG_H_

#include <zephyr/sys/util.h>

/* --- identity ------------------------------------------------------------ */
#define LSM6_REG_WHO_AM_I      0x0FU
#define LSM6_VAL_WHO_AM_I      0x6AU

/* --- interrupt source ---------------------------------------------------- */
#define LSM6_REG_WAKE_UP_SRC   0x1BU
#define LSM6_WAKE_UP_SRC_FF_IA BIT(5)  /* free-fall event detected           */
#define LSM6_WAKE_UP_SRC_WU_IA BIT(3)  /* wake-up event detected             */

/* --- control ------------------------------------------------------------- */
#define LSM6_REG_CTRL1_XL      0x10U  /* ODR_XL[7:4] FS_XL[3:2] ...          */
#define LSM6_CTRL1_FS_XL_MASK  0x0CU
#define LSM6_CTRL1_FS_XL_16G   (1U << 2)  /* fs_map index 1 == +/-16 g       */

#define LSM6_REG_CTRL2_G       0x11U  /* ODR_G[7:4] FS_G[3:2] FS_125[1]      */
#define LSM6_CTRL2_FS_G_MASK   0x0CU
#define LSM6_CTRL2_FS_G_2000   (3U << 2)  /* fs_map index 3 == +/-2000 dps   */
#define LSM6_CTRL2_FS_125      BIT(1)     /* must be 0 for FS_G to apply     */

/* --- embedded function enables ------------------------------------------- */
#define LSM6_REG_TAP_CFG       0x58U
#define LSM6_TAP_CFG_INT_EN    BIT(7)  /* master enable for basic interrupts */
#define LSM6_TAP_CFG_LIR       BIT(0)  /* latch until WAKE_UP_SRC is read    */

/* --- free-fall ----------------------------------------------------------- */
/*
 * FF_DUR is split across two registers: the low 5 bits live in FREE_FALL[7:3]
 * and the 6th bit in WAKE_UP_DUR[7]. Duration is expressed in accelerometer
 * ODR periods, so the time it represents changes with ODR.
 */
#define LSM6_REG_WAKE_UP_DUR   0x5CU
#define LSM6_WAKE_UP_DUR_FF5   BIT(7)  /* FF_DUR bit 5                       */

#define LSM6_REG_FREE_FALL     0x5DU
#define LSM6_FF_DUR_LO_MASK    0xF8U  /* FF_DUR[4:0] in bits 7:3             */
#define LSM6_FF_DUR_LO_SHIFT   3U     /* NOT 4 - see the header comment      */
#define LSM6_FF_THS_MASK       0x07U  /* FF_THS[2:0] in bits 2:0             */

#define LSM6_FF_THS_MAX        7U
#define LSM6_FF_DUR_MAX        63U    /* 6 bits total                        */

/*
 * ASSUMED - labels only, nothing depends on these numerically.
 * Commonly cited FF_THS code -> threshold mapping for this part family.
 * CONFIRM against DS12742 before treating as fact.
 */
#define LSM6_FF_THS_MG_ASSUMED { 156, 219, 250, 312, 344, 406, 469, 500 }

/* --- interrupt routing --------------------------------------------------- */
#define LSM6_REG_MD1_CFG       0x5EU
#define LSM6_MD1_CFG_INT1_FF   BIT(4)  /* route free-fall to INT1            */
#define LSM6_MD1_CFG_INT1_WU   BIT(5)  /* route wake-up to INT1              */

#endif /* LSM6DS3TR_REG_H_ */
