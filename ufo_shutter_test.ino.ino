const int SHUTTER_PIN = 8;   // D8 -> 1k resistor -> controller pin 10

// set this depending on what you see in testing
const bool OPEN_STATE  = LOW;   // or HIGH
const bool CLOSED_STATE = !OPEN_STATE;

void setup() {
  pinMode(SHUTTER_PIN, OUTPUT);
  digitalWrite(SHUTTER_PIN, CLOSED_STATE);  // make sure shutter starts closed
}

void loop() {
  // Example: 1 second exposure every 5 seconds
  openShutter();
  delay(1000);      // exposure time in ms
  closeShutter();
  delay(4000);      // gap between exposures
}

void openShutter() {
  digitalWrite(SHUTTER_PIN, OPEN_STATE);
}

void closeShutter() {
  digitalWrite(SHUTTER_PIN, CLOSED_STATE);
}
