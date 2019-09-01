import asyncio
import websockets


async def listen():
    async with websockets.connect('ws://localhost:8765/sub') as websocket:
        while True:
            greeting = await websocket.recv()
            print("< {}".format(greeting))


asyncio.get_event_loop().run_until_complete(listen())
