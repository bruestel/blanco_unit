#!/usr/bin/env python3
"""MQTT Bridge for Blanco Unit Bluetooth Integration."""

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from typing import Any

import paho.mqtt.client as mqtt
from bleak import BleakScanner
from dotenv import load_dotenv

# --- Blanco Integration Module Loading ---
# This ensures we can load the custom_components modules without a full HA environment.
sys.path.insert(0, os.path.join(os.getcwd(), "custom_components"))

def load_blanco_module(name: str, path: str) -> Any:
    """Dynamically load a Blanco Unit module for standalone use."""
    spec = importlib.util.spec_from_file_location(f"blanco_unit.{name}", path)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"blanco_unit.{name}"] = module
        spec.loader.exec_module(module)
        return module
    raise ImportError(f"Could not load module {name} from {path}")

# Load necessary Blanco modules
load_blanco_module("const", "custom_components/blanco_unit/const.py")
load_blanco_module("data", "custom_components/blanco_unit/data.py")
client_mod = load_blanco_module("client", "custom_components/blanco_unit/client.py")
BlancoUnitBluetoothClient = client_mod.BlancoUnitBluetoothClient

# --- Configuration & Logging ---
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
_LOGGER = logging.getLogger("blanco_mqtt")

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASSWORD")
BLANCO_MAC = os.getenv("BLANCO_MAC")
BLANCO_PIN = os.getenv("BLANCO_PIN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 900))

# --- Topic Structure ---
MAC_ID = BLANCO_MAC.replace(":", "").lower() if BLANCO_MAC else "unknown"
BASE_TOPIC = f"blanco/{MAC_ID}"
TOPIC_STATUS = f"{BASE_TOPIC}/status"
TOPIC_CMD_DISPENSE = f"{BASE_TOPIC}/command/dispense"
TOPIC_CMD_REFRESH = f"{BASE_TOPIC}/command/refresh"
TOPIC_CMD_RESULT = f"{BASE_TOPIC}/command/result"
TOPIC_AVAILABLE = f"{BASE_TOPIC}/available"

TAP_STATE_MAP = {
    0: "Normal",
    1: "Dispensing",
    2: "Error",
    3: "Cleaning",
    4: "Locked"
}

class BlancoMQTTBridge:
    """Bridge between Blanco Unit Bluetooth and MQTT."""

    def __init__(self) -> None:
        """Initialize the MQTT bridge."""
        self.blanco_client = None
        self.device = None
        self.loop = None
        self.device_info = {}
        self._is_fast_polling = False
        self._conn_lock = asyncio.Lock()
        
        # Generate unique client ID for the session
        client_id = f"blanco_bridge_{MAC_ID}"
        self.mqtt_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2, 
            client_id=client_id
        )
        
        if MQTT_USER:
            self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        
        # Last Will and Testament (LWT)
        self.mqtt_client.will_set(TOPIC_AVAILABLE, payload="offline", qos=1, retain=True)
        
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message

    def on_mqtt_connect(self, client, userdata, flags, rc, properties=None) -> None:
        """Handle MQTT connection event."""
        if rc == 0:
            _LOGGER.info("Connected to MQTT Broker. Base topic: %s", BASE_TOPIC)
            client.subscribe(TOPIC_CMD_DISPENSE)
            client.subscribe(TOPIC_CMD_REFRESH)
            client.publish(TOPIC_AVAILABLE, "online", retain=True)
        else:
            _LOGGER.error("MQTT Connection failed with code %s", rc)

    def on_mqtt_message(self, client, userdata, msg) -> None:
        """Handle incoming MQTT messages."""
        try:
            if msg.topic == TOPIC_CMD_REFRESH:
                _LOGGER.info("Received refresh command")
                if self.loop:
                    asyncio.run_coroutine_threadsafe(self.update_status(), self.loop)
                return

            payload = json.loads(msg.payload)
            if msg.topic == TOPIC_CMD_DISPENSE:
                amount = payload.get("amount", 200)
                intensity = payload.get("intensity", 1)
                _LOGGER.info("Received dispense command: %dml, intensity %d", amount, intensity)
                
                # Execute dispense in the running event loop
                if self.loop:
                    asyncio.run_coroutine_threadsafe(self.dispense(amount, intensity), self.loop)
        except Exception as e:
            _LOGGER.error("Failed to parse MQTT message: %s", e)

    async def ensure_connected(self, force_rescan: bool = False) -> bool:
        """Ensure we have a valid client and device, scanning if needed."""
        if not force_rescan and self.blanco_client and self.blanco_client.is_connected:
            return True

        async with self._conn_lock:
            # Re-check inside lock
            if not force_rescan and self.blanco_client and self.blanco_client.is_connected:
                return True

            _LOGGER.info("Ensuring connection to Blanco Unit (Force Rescan=%s)...", force_rescan)
            
            try:
                if force_rescan or not self.device:
                    _LOGGER.info("Scanning for Blanco Unit %s...", BLANCO_MAC)
                    self.device = await BleakScanner.find_device_by_address(BLANCO_MAC, timeout=20.0)
                    
                    if not self.device:
                        _LOGGER.error("Blanco Unit not found during scan!")
                        return False
                    
                    _LOGGER.info("Device found. Re-initializing client...")
                    self.blanco_client = BlancoUnitBluetoothClient(
                        pin=BLANCO_PIN,
                        device=self.device,
                        connection_callback=lambda connected: _LOGGER.info(
                            "Bluetooth Status: %s", "CONNECTED" if connected else "DISCONNECTED"
                        )
                    )
                
                return True
            except Exception as e:
                _LOGGER.error("Error during connection setup: %s", e)
                return False

    async def dispense(self, amount: int, intensity: int) -> None:
        """Execute water dispensing and trigger fast polling."""
        if not await self.ensure_connected():
            _LOGGER.warning("Could not ensure connection for dispensing")
            return
            
        try:
            success = await self.blanco_client.dispense_water(amount, intensity)
            _LOGGER.info("Dispensing %s", "started" if success else "failed")
            self.mqtt_client.publish(TOPIC_CMD_RESULT, "success" if success else "failed")
            
            # Start fast polling after successful dispense to monitor progress
            if success:
                asyncio.create_task(self.fast_poll_status(duration=30, interval=2))
        except Exception as e:
            _LOGGER.error("Error during dispensing: %s", e)
            self.mqtt_client.publish(TOPIC_CMD_RESULT, f"error: {e}")
            # If we get a connection error, force a rescan next time
            if "not connected" in str(e).lower() or "0x0e" in str(e):
                await self.ensure_connected(force_rescan=True)

    async def fetch_device_info(self) -> None:
        """Fetch static device information."""
        if not await self.ensure_connected():
            return

        try:
            _LOGGER.info("Fetching device info...")
            sys_info = await self.blanco_client.get_system_info()
            identity = await self.blanco_client.get_device_identity()
            wifi_info = await self.blanco_client.get_wifi_info()

            self.device_info = {
                "device_name": sys_info.dev_name,
                "serial_number": identity.serial_no,
                "service_code": identity.service_code,
                "firmware": {
                    "main": sys_info.sw_ver_main_con,
                    "comm": sys_info.sw_ver_comm_con,
                    "elec": sys_info.sw_ver_elec_con
                },
                "wifi": {
                    "ssid": wifi_info.ssid,
                    "ip": wifi_info.ip,
                    "signal": wifi_info.signal,
                    "cloud_connected": wifi_info.cloud_connect
                }
            }
            _LOGGER.info("Device info fetched: %s", self.device_info["device_name"])
        except Exception as e:
            _LOGGER.error("Failed to fetch device info: %s", e)

    async def update_status(self, retries: int = 2) -> None:
        """Perform a single status update and publish to MQTT with retries."""
        for attempt in range(retries + 1):
            try:
                # Force rescan if this is a retry
                if not await self.ensure_connected(force_rescan=(attempt > 0)):
                    _LOGGER.warning("Status update attempt %d: connection failed", attempt + 1)
                    await asyncio.sleep(2)
                    continue

                self.mqtt_client.publish(TOPIC_AVAILABLE, "online", retain=True)
                _LOGGER.info("Updating Blanco Unit status (Attempt %d/%d)...", attempt + 1, retries + 1)
                
                status = await self.blanco_client.get_status()
                
                data = {
                    "device": self.device_info,
                    "status": {
                        "filter_rest": status.filter_rest,
                        "co2_rest": status.co2_rest,
                        "temp_boiler_1": status.temp_boil_1,
                        "temp_boiler_2": status.temp_boil_2,
                        "temp_compressor": status.temp_comp,
                        "is_dispensing": status.wtr_disp_active,
                        "tap_state": status.tap_state,
                        "tap_state_desc": TAP_STATE_MAP.get(status.tap_state, "Unknown"),
                        "error_bits": status.err_bits,
                    },
                    "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                
                self.mqtt_client.publish(TOPIC_STATUS, json.dumps(data), retain=True)
                _LOGGER.info("Status sent to MQTT.")
                return  # Success!
                
            except Exception as e:
                _LOGGER.warning("Status update attempt %d failed: %s", attempt + 1, e)
                if attempt < retries:
                    await asyncio.sleep(5)
                    if "0x0e" in str(e) or "Disconnected" in str(e) or "not connected" in str(e).lower():
                         _LOGGER.info("Connection error detected, forcing disconnect before retry...")
                         try:
                             await self.blanco_client.disconnect()
                         except:
                             pass
                else:
                    _LOGGER.error("All status update attempts failed.")

    async def fast_poll_status(self, duration: int = 30, interval: int = 2) -> None:
        """Perform rapid status updates for a limited duration."""
        if self._is_fast_polling:
            return
        
        _LOGGER.info("Starting fast status polling for %ds...", duration)
        self._is_fast_polling = True
        end_time = time.time() + duration
        
        try:
            while time.time() < end_time:
                await self.update_status(retries=0)
                await asyncio.sleep(interval)
        finally:
            self._is_fast_polling = False
            _LOGGER.info("Fast status polling finished.")

    async def poll_status(self) -> None:
        """Periodic status update via periodic connection."""
        while True:
            if not self._is_fast_polling:
                await self.update_status()
            await asyncio.sleep(POLL_INTERVAL)

    async def run(self) -> None:
        """Start the bridge and run the main loop."""
        self.loop = asyncio.get_running_loop()
        
        if await self.ensure_connected():
            await self.fetch_device_info()
        else:
            _LOGGER.warning("Initial connection failed, will retry during polling")

        _LOGGER.info("Connecting to MQTT Broker at %s:%d...", MQTT_BROKER, MQTT_PORT)
        try:
            self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
            self.mqtt_client.loop_start()
        except Exception as e:
            _LOGGER.error("MQTT Connection failed: %s", e)
            return

        await self.poll_status()

    async def stop(self) -> None:
        """Gracefully stop the bridge and disconnect clients."""
        _LOGGER.info("Shutting down bridge...")
        try:
            self.mqtt_client.publish(TOPIC_AVAILABLE, "offline", retain=True)
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        except:
            pass
            
        if self.blanco_client:
            try:
                await self.blanco_client.disconnect()
            except:
                pass

if __name__ == "__main__":
    bridge = BlancoMQTTBridge()
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        asyncio.run(bridge.stop())
    except Exception as e:
        _LOGGER.critical("Critical error: %s", e)
