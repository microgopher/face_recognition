import asyncio
import websockets

connections = set()
n = 0


async def handler(websocket, path):
    global n

    if path == "/sub":
        n = n + 1
        i = n
        connections.add(websocket)
        print("adding subscriber #", i)
        try:
            while True:
                message = await websocket.recv()
                
        except websockets.ConnectionClosed:
            pass
        finally:
            print("removing subscriber #", i)
            connections.remove(websocket)

    elif path == "/pub":
        while True:
            try:
                msg = await websocket.recv()
            except websockets.ConnectionClosed:
                break
            print("<", msg)
            for ws in connections:
                asyncio.ensure_future(ws.send(msg))


start_server = websockets.serve(handler, 'localhost', 8765)

asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
