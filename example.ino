#include "driver/i2s.h"
#define SAMPLE_RATE (44100)

#define I2S_MIC_WS 12
#define I2S_MIC_SD 14
#define I2S_MIC_BCK 13
#define I2S_PORT I2S_NUM_0
#define bufferLen 64

int16_t sBuffer[bufferLen];

  // 安装I2S驱动
  void i2s_install(){

    // 配置I2S接收
    i2s_config_t i2s_config = {
      .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
      .sample_rate = SAMPLE_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,  
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = (i2s_comm_format_t)(I2S_COMM_FORMAT_I2S | I2S_COMM_FORMAT_I2S_MSB),
      .intr_alloc_flags = 0,
      .dma_buf_count = 16,
      .dma_buf_len = bufferLen,
      .use_apll = false      
   };
   if (ESP_OK != i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL)) {
      Serial.println("Install I2S driver failed");
      return;
     }  
}

  // 配置I2S引脚
  void i2s_setpin(){

    i2s_pin_config_t pin_config = {};
    pin_config.bck_io_num = I2S_MIC_BCK;
    pin_config.ws_io_num = I2S_MIC_WS;
    pin_config.data_out_num = I2S_PIN_NO_CHANGE;
    pin_config.data_in_num = I2S_MIC_SD;

   if (ESP_OK != i2s_set_pin(I2S_PORT, &pin_config)) {
    Serial.println("I2S set pin failed");
    return;
  }
 }

void setup() {

  Serial.begin(115200);
  Serial.println("Setup I2S ...");

  delay(1000);
  i2s_install();
  i2s_setpin();
  i2s_start(I2S_PORT);
  delay(500);  
}


void loop() {
   size_t bytesIn = 0;
  esp_err_t result = i2s_read(I2S_PORT, &sBuffer, bufferLen, &bytesIn, portMAX_DELAY);
  if (result == ESP_OK)
  {
    int samples_read = bytesIn / 2;
    if (samples_read > 0) {
      float mean = 0;
      for (int i = 0; i < samples_read; ++i) {
        mean += (sBuffer[i]);
      }
      mean /= samples_read;
      Serial.println(mean);
      delay(50);
    }
  }

}

