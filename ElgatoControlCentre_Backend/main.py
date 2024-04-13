import requests
import json

url = "http://192.168.0.10:9123/elgato/lights"

# Application Entry Point
if __name__ == '__main__':
    print("Enter the number of lights")

    numberOfLights = int(input("Number of Lights: "))

    print("Power status of the light, enter 1 to turn on and 0 to turn off")

    lightPower = int(input("Power status of the device: "))

    print("Enter the light brightness value")

    lightBrightness = int(input("Brightness Value: "))

    print("Enter the light temperature value")

    lightTemperature = int(input("Temperature Value: "))

    lights = []

    lights.append({
        "on": lightPower,
        "brightness": lightBrightness,
        "temperature": lightTemperature
    })

    '''
    payload = json.dumps({
        "numberOfLights": 1,
        "lights": [
            {
                "on": 0,
                "brightness": 32,
                "temperature": 227
            }
        ]
    })
    headers = {
        'Content-Type': 'application/json'
    }
    
    response = requests.request("PUT", url, headers=headers, data=payload)
    
    '''

    payload = json.dumps({
        "numberOfLights": numberOfLights,
        "lights": lights
    })
    headers = {
        'Content-Type': 'application/json'
    }

    response = requests.request("PUT", url, headers=headers, data=payload)

    print(response.text)