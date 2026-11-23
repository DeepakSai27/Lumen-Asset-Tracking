/* Wi-Fi station with automatic reconnect. */
#include <string.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/event_groups.h"
#include "wifi.h"

static const char *TAG = "wifi";
static EventGroupHandle_t s_events;
#define CONNECTED_BIT BIT0

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
#if CONFIG_LUMEN_WIFI_2G_ONLY && CONFIG_SOC_WIFI_SUPPORT_5G
        /* Must be set after esp_wifi_start() and before connecting. */
        esp_err_t err = esp_wifi_set_band_mode(WIFI_BAND_MODE_2G_ONLY);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "could not force 2.4 GHz: %s", esp_err_to_name(err));
        }
#endif
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, CONNECTED_BIT);
        wifi_event_sta_disconnected_t *d = data;
        ESP_LOGW(TAG, "disconnected (reason %d), reconnecting", d->reason);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "connected, IP " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_events, CONNECTED_BIT);
    }
}

void wifi_start(void)
{
    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(netif, "lumen-checkpoint");

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));

    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, CONFIG_LUMEN_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_LUMEN_WIFI_PASSWORD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = strlen(CONFIG_LUMEN_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.pmf_cfg.capable = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Maximum Wi-Fi transmit power (units of 0.25 dBm: 80 = 20 dBm). */
    esp_err_t perr = esp_wifi_set_max_tx_power(80);
    int8_t pwr = 0;
    esp_wifi_get_max_tx_power(&pwr);
    ESP_LOGI(TAG, "Wi-Fi TX power %s, now %d.%02d dBm", perr == ESP_OK ? "set to max" : "not changed",
             pwr / 4, (pwr % 4) * 25);
    ESP_LOGI(TAG, "connecting to \"%s\"", CONFIG_LUMEN_WIFI_SSID);
}

bool wifi_wait_connected(TickType_t timeout)
{
    return xEventGroupWaitBits(s_events, CONNECTED_BIT, pdFALSE, pdTRUE, timeout) & CONNECTED_BIT;
}
