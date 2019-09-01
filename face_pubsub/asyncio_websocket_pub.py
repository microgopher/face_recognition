#import asyncio
import websockets
import json

#async def say():
#    async with websockets.connect('ws://localhost:8765/pub') as websocket:
#        msg = "Hello testing"
#        await websocket.send(msg)


#asyncio.get_event_loop().run_until_complete(say())


from websocket import create_connection
ws = create_connection("ws://localhost:8765/pub")
data = {'topic': 'new', "face_id": "5ae8b8ea5338db41931a9800", "face_url": "http://localhost:8010/face_image/5ae8b8ea5338db41931a9800"}
data1 = {"topic": "new", "face_id": "5ae8b8ea5338db41931a9801","face_url": "http://localhost:8010/face_image/5ae8b8ea5338db41931a9801"}
data2 = {"topic": "new", "face_id": "5ae8b8ea5338db41931a9802", "face_url": "http://localhost:8010/face_image/5ae8b8ea5338db41931a9802"}
ws.send(json.dumps(data))
ws.send(json.dumps(data1))
ws.send(json.dumps(data2))
ws.close()
