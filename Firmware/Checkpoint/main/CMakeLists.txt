idf_component_register(
    SRCS "main.c" "wifi.c" "ble.c" "report.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_client esp_timer
                  esp_app_format bt json
)
