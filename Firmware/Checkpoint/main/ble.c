/*
 * BLE side of the checkpoint (NimBLE):
 *  - passive scan for employee tags, keeping the strongest RSSI per tag per report window
 *  - optional iBeacon advertising so the checkpoint is itself a beacon
 */
#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "ble.h"

static const char *TAG = "ble";

static sighting_t        s_table[BLE_MAX_SIGHTINGS];
static size_t            s_count;
static uint32_t          s_dropped;
static uint8_t           s_white[BLE_MAX_WHITELIST][6];
static size_t            s_white_count;
static bool              s_discover;
static SemaphoreHandle_t s_lock;
static uint8_t           s_own_addr_type;

static int gap_event(struct ble_gap_event *ev, void *arg);

/* Find an iBeacon frame in raw advertising data. */
static bool parse_ibeacon(const uint8_t *d, uint8_t len, sighting_t *s)
{
    uint8_t i = 0;
    while (i + 1 < len) {
        uint8_t l = d[i];
        if (l == 0 || i + 1 + l > len) {
            break;
        }
        uint8_t type = d[i + 1];
        const uint8_t *p = &d[i + 2];
        uint8_t pl = l - 1;
        /* Manufacturer data: Apple (0x004C), type 0x02, length 0x15 */
        if (type == 0xFF && pl >= 25 && p[0] == 0x4C && p[1] == 0x00 && p[2] == 0x02 && p[3] == 0x15) {
            memcpy(s->uuid, &p[4], 16);
            s->major = (uint16_t)((p[20] << 8) | p[21]);
            s->minor = (uint16_t)((p[22] << 8) | p[23]);
            return true;
        }
        i += l + 1;
    }
    return false;
}

static bool is_whitelisted(const uint8_t mac[6])
{
    for (size_t i = 0; i < s_white_count; i++) {
        if (memcmp(s_white[i], mac, 6) == 0) {
            return true;
        }
    }
    return false;
}

static void on_adv(const uint8_t *addr_le, int8_t rssi, const uint8_t *data, uint8_t len)
{
    if (rssi < CONFIG_LUMEN_MIN_RSSI) {
        return;
    }
    sighting_t s = { .rssi = rssi };
    for (int i = 0; i < 6; i++) {
        s.mac[i] = addr_le[5 - i];          /* NimBLE stores the address little-endian */
    }
    s.has_ib = parse_ibeacon(data, len, &s);

    xSemaphoreTake(s_lock, portMAX_DELAY);
    bool wanted = s.has_ib || is_whitelisted(s.mac) ||
                  (s_discover && rssi >= CONFIG_LUMEN_DISCOVER_MIN_RSSI);
    if (wanted) {
        s.seen_us = esp_timer_get_time();
        size_t i;
        for (i = 0; i < s_count; i++) {
            if (memcmp(s_table[i].mac, s.mac, 6) == 0) {
                break;
            }
        }
        if (i < s_count) {
            if (rssi > s_table[i].rssi) {
                s_table[i].rssi = rssi;
            }
            s_table[i].seen_us = s.seen_us;
            if (s.has_ib) {
                s_table[i].has_ib = true;
                memcpy(s_table[i].uuid, s.uuid, 16);
                s_table[i].major = s.major;
                s_table[i].minor = s.minor;
            }
        } else if (s_count < BLE_MAX_SIGHTINGS) {
            s_table[s_count++] = s;
        } else {
            s_dropped++;
        }
    }
    xSemaphoreGive(s_lock);
}

static void start_scan(void)
{
    struct ble_gap_disc_params p = {
        .itvl = 160,              /* 100 ms (0.625 ms units)            */
        .window = 80,             /* 50 ms: leaves air time for Wi-Fi   */
        .filter_policy = 0,
        .limited = 0,
        .passive = 1,
        .filter_duplicates = 0,   /* we want every advert for RSSI      */
    };
    int rc = ble_gap_disc(s_own_addr_type, BLE_HS_FOREVER, &p, gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gap_disc failed: %d", rc);
    }
}

#if CONFIG_LUMEN_ADV_ENABLE
static bool parse_uuid(const char *str, uint8_t out[16])
{
    int n = 0;
    for (const char *c = str; *c && n < 32; c++) {
        int v;
        if (*c >= '0' && *c <= '9') v = *c - '0';
        else if (*c >= 'a' && *c <= 'f') v = *c - 'a' + 10;
        else if (*c >= 'A' && *c <= 'F') v = *c - 'A' + 10;
        else continue;                       /* skip dashes */
        if (n % 2 == 0) out[n / 2] = (uint8_t)(v << 4);
        else out[n / 2] |= (uint8_t)v;
        n++;
    }
    return n == 32;
}

static void start_adv(void)
{
    uint8_t adv[30] = {
        0x02, 0x01, 0x06,                         /* flags: LE general, no BR/EDR */
        0x1A, 0xFF, 0x4C, 0x00, 0x02, 0x15,       /* iBeacon prefix               */
    };
    if (!parse_uuid(CONFIG_LUMEN_ADV_UUID, &adv[9])) {
        ESP_LOGE(TAG, "LUMEN_ADV_UUID is not a valid UUID, not advertising");
        return;
    }
    adv[25] = (uint8_t)(CONFIG_LUMEN_ADV_MAJOR >> 8);
    adv[26] = (uint8_t)(CONFIG_LUMEN_ADV_MAJOR & 0xFF);
    adv[27] = (uint8_t)(CONFIG_LUMEN_ADV_MINOR >> 8);
    adv[28] = (uint8_t)(CONFIG_LUMEN_ADV_MINOR & 0xFF);
    adv[29] = (uint8_t)(int8_t)CONFIG_LUMEN_ADV_MEASURED_POWER;

    int rc = ble_gap_adv_set_data(adv, sizeof(adv));
    if (rc != 0) {
        ESP_LOGE(TAG, "adv_set_data failed: %d", rc);
        return;
    }
    struct ble_gap_adv_params ap = {
        .conn_mode = BLE_GAP_CONN_MODE_NON,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
        .itvl_min = CONFIG_LUMEN_ADV_INTERVAL_MS * 1000 / 625,
        .itvl_max = CONFIG_LUMEN_ADV_INTERVAL_MS * 1000 / 625,
    };
    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &ap, NULL, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv_start failed: %d", rc);
    } else {
        ESP_LOGI(TAG, "advertising iBeacon major=%d minor=%d",
                 CONFIG_LUMEN_ADV_MAJOR, CONFIG_LUMEN_ADV_MINOR);
    }
}
#endif

static int gap_event(struct ble_gap_event *ev, void *arg)
{
    switch (ev->type) {
    case BLE_GAP_EVENT_DISC:
        on_adv(ev->disc.addr.val, ev->disc.rssi, ev->disc.data, ev->disc.length_data);
        return 0;
    case BLE_GAP_EVENT_DISC_COMPLETE:
        start_scan();             /* should not happen with BLE_HS_FOREVER; restart anyway */
        return 0;
    default:
        return 0;
    }
}

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc == 0) {
        rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "no usable BLE address: %d", rc);
        return;
    }
#if CONFIG_LUMEN_ADV_ENABLE
    start_adv();
#endif
    start_scan();
    ESP_LOGI(TAG, "scanning");
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "BLE host reset, reason %d", reason);
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

esp_err_t ble_start(void)
{
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %s", esp_err_to_name(err));
        return err;
    }
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
    return ESP_OK;
}

size_t ble_take_sightings(sighting_t *out, size_t max, uint32_t *dropped)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    size_t n = s_count < max ? s_count : max;
    memcpy(out, s_table, n * sizeof(sighting_t));
    s_count = 0;
    if (dropped) {
        *dropped = s_dropped;
    }
    xSemaphoreGive(s_lock);
    return n;
}

void ble_set_filter(const uint8_t (*macs)[6], size_t n, bool discover)
{
    if (n > BLE_MAX_WHITELIST) {
        n = BLE_MAX_WHITELIST;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    memcpy(s_white, macs, n * 6);
    s_white_count = n;
    s_discover = discover;
    xSemaphoreGive(s_lock);
}
