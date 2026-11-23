#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define BLE_MAX_SIGHTINGS 128   /* distinct tags buffered per report window */
#define BLE_MAX_WHITELIST 256   /* registered tag MACs pushed by the server  */

typedef struct {
    uint8_t  mac[6];     /* big-endian, as printed AA:BB:...  */
    int8_t   rssi;       /* strongest RSSI in this window     */
    int64_t  seen_us;    /* esp_timer time of the last advert */
    bool     has_ib;
    uint8_t  uuid[16];
    uint16_t major;
    uint16_t minor;
} sighting_t;

/* Start NimBLE: scanning and (if enabled) iBeacon advertising. NVS must be initialised. */
esp_err_t ble_start(void);

/* Move the buffered sightings into out[] and clear the buffer. Returns the count. */
size_t ble_take_sightings(sighting_t *out, size_t max, uint32_t *dropped);

/* Which non-iBeacon devices to report: registered MACs, plus anything close
 * when the server has "scan for new tags" switched on. */
void ble_set_filter(const uint8_t (*macs)[6], size_t n, bool discover);
