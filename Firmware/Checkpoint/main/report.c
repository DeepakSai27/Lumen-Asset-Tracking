/*
 * Reporting: every CONFIG_LUMEN_REPORT_INTERVAL_MS, POST the buffered sightings
 * to <server>/api/checkpoint/report as JSON. The reply carries the list of
 * registered tag MACs and the "scan for new tags" flag, which feed the BLE filter.
 *
 * Request:  {"checkpoint":"AA:BB:..","name":"..","fw":"0.1.0","uptime_s":123,"dropped":0,
 *            "sightings":[{"mac":"..","rssi":-60,"age_ms":1200,
 *                          "ib":{"uuid":"..","major":1,"minor":2}}]}
 * Response: {"whitelist":["AA:BB:..",...],"discover":false,"server_time":...}
 */
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ble.h"
#include "report.h"
#include "wifi.h"

static const char *TAG = "report";

static sighting_t s_buf[BLE_MAX_SIGHTINGS];
static char       s_resp[8192];
static uint8_t    s_white[BLE_MAX_WHITELIST][6];
static char       s_id[18];

static void fmt_mac(const uint8_t m[6], char out[18])
{
    snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static void fmt_uuid(const uint8_t u[16], char out[37])
{
    snprintf(out, 37,
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7],
             u[8], u[9], u[10], u[11], u[12], u[13], u[14], u[15]);
}

static char *build_body(size_t n, uint32_t dropped)
{
    int64_t now = esp_timer_get_time();
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "checkpoint", s_id);
    cJSON_AddStringToObject(root, "name", CONFIG_LUMEN_CHECKPOINT_NAME);
    cJSON_AddStringToObject(root, "fw", esp_app_get_description()->version);
    cJSON_AddNumberToObject(root, "uptime_s", (double)(now / 1000000));
    cJSON_AddNumberToObject(root, "dropped", dropped);
    cJSON *arr = cJSON_AddArrayToObject(root, "sightings");
    for (size_t i = 0; i < n; i++) {
        char mac[18];
        fmt_mac(s_buf[i].mac, mac);
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "mac", mac);
        cJSON_AddNumberToObject(o, "rssi", s_buf[i].rssi);
        cJSON_AddNumberToObject(o, "age_ms", (double)((now - s_buf[i].seen_us) / 1000));
        if (s_buf[i].has_ib) {
            char uuid[37];
            fmt_uuid(s_buf[i].uuid, uuid);
            cJSON *ib = cJSON_AddObjectToObject(o, "ib");
            cJSON_AddStringToObject(ib, "uuid", uuid);
            cJSON_AddNumberToObject(ib, "major", s_buf[i].major);
            cJSON_AddNumberToObject(ib, "minor", s_buf[i].minor);
        }
        cJSON_AddItemToArray(arr, o);
    }
    char *body = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return body;
}

static void apply_response(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) {
        ESP_LOGW(TAG, "unparseable response");
        return;
    }
    size_t n = 0;
    const cJSON *wl = cJSON_GetObjectItem(root, "whitelist");
    const cJSON *item;
    cJSON_ArrayForEach(item, wl) {
        if (n >= BLE_MAX_WHITELIST || !cJSON_IsString(item)) {
            continue;
        }
        unsigned int b[6];
        if (sscanf(item->valuestring, "%2x:%2x:%2x:%2x:%2x:%2x",
                   &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) == 6) {
            for (int i = 0; i < 6; i++) {
                s_white[n][i] = (uint8_t)b[i];
            }
            n++;
        }
    }
    bool discover = cJSON_IsTrue(cJSON_GetObjectItem(root, "discover"));
    ble_set_filter((const uint8_t (*)[6])s_white, n, discover);
    cJSON_Delete(root);
}

static bool post(const char *body)
{
    esp_http_client_config_t cfg = {
        .url = CONFIG_LUMEN_SERVER_URL "/api/checkpoint/report",
        .method = HTTP_METHOD_POST,
        .timeout_ms = 5000,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) {
        return false;
    }
    esp_http_client_set_header(c, "Content-Type", "application/json");
    esp_http_client_set_header(c, "Authorization", "Bearer " CONFIG_LUMEN_API_TOKEN);

    bool ok = false;
    int len = (int)strlen(body);
    esp_err_t err = esp_http_client_open(c, len);
    if (err == ESP_OK && esp_http_client_write(c, body, len) == len) {
        esp_http_client_fetch_headers(c);
        int status = esp_http_client_get_status_code(c);
        int r = esp_http_client_read_response(c, s_resp, sizeof(s_resp) - 1);
        s_resp[r > 0 ? r : 0] = '\0';
        if (status == 200) {
            apply_response(s_resp);
            ok = true;
        } else {
            ESP_LOGW(TAG, "server replied %d: %.120s", status, s_resp);
        }
    } else {
        ESP_LOGW(TAG, "POST failed: %s", esp_err_to_name(err));
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return ok;
}

static void report_task(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(CONFIG_LUMEN_REPORT_INTERVAL_MS));
        if (!wifi_wait_connected(0)) {
            continue;   /* keep buffering; strongest RSSI and latest time are kept per tag */
        }
        uint32_t dropped = 0;
        size_t n = ble_take_sightings(s_buf, BLE_MAX_SIGHTINGS, &dropped);
        char *body = build_body(n, dropped);
        if (!body) {
            ESP_LOGE(TAG, "out of memory building report");
            continue;
        }
        bool ok = post(body);
        ESP_LOGI(TAG, "%u tag(s) -> %s", (unsigned)n, ok ? "ok" : "FAILED");
        cJSON_free(body);
    }
}

void report_start(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    fmt_mac(mac, s_id);
    ESP_LOGI(TAG, "checkpoint id %s, server %s", s_id, CONFIG_LUMEN_SERVER_URL);
    xTaskCreate(report_task, "report", 6144, NULL, 5, NULL);
}
