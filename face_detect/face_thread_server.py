import io
import socket
from threading import *
import struct
import cv2
import numpy
from PIL import Image
from subprocess import Popen, PIPE
import re

from detect import *
from pymongo import MongoClient

client = MongoClient()
db = client.devices


# Start a socket listening for connections on 0.0.0.0:8000 (0.0.0.0 means
# all interfaces)
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

server_socket.bind(('0.0.0.0', 8000))
FACE_DIR = "../faces"

class ClientThread(Thread):
    def __init__(self, clientAddress, clientsocket):
        Thread.__init__(self)
        self.csocket = clientsocket
        print ("New connection added: ", clientAddress)
        #mac = self.get_mac_address(clientAddress[0])
        #self.device_uuid = mac and mac.replace(":","") or clientAddress[0].replace(".","")
        #self.device_mac = mac
        #self.device_ip = clientAddress[0]
        #print ("mac: ", mac)
    def run(self):
        connection = self.csocket.makefile('rb')
        mac = struct.unpack('<Q', connection.read(struct.calcsize('<Q')))[0]
        self.device_uuid = mac
        self.device_mac = hex(mac)
        print (self.device_mac)
        while True:
            # Read the length of the image as a 32-bit unsigned int. If the
            # length is zero, quit the loop
            image_len = struct.unpack('<L', connection.read(struct.calcsize('<L')))[0]
            if not image_len:
                break
            # Construct a stream to hold the image data and read the image
            # data from the connection
            image_stream = io.BytesIO()
            image_stream.write(connection.read(image_len))
            # Rewind the stream, open it as an image with PIL and do some
            # processing on it
            image_stream.seek(0)
            image = Image.open(image_stream)
            #print('Image is %dx%d' % image.size)
            image.verify()
            #print('Image is verified')
            #Convert the picture into a numpy array
            buff = numpy.fromstring(image_stream.getvalue(), dtype=numpy.uint8)

            #Now creates an OpenCV image
            image = cv2.imdecode(buff, 1)
            tgtdir = "%s/%s" % (FACE_DIR, self.device_uuid)
            face_detect = ObjectDetectorCascadeOpenCV('haarcascade_frontalface_default.xml', tgtdir=tgtdir)
            face_files, rectangles = face_detect.run(image)
            for face_file in face_files:
                image = Image.open(face_file)
                
                
                data = {
                  
                  'face_path': face_file,
                  'age': False,
                  'gender': False,
                  'device_uuid': self.device_uuid,
                  'device_mac': self.device_mac,
                  'lock_age': False,
                  'lock_gender': False,
                  'parent': False
                }
                res = db.faces.insert_one(data)
        connection.close()

while True:
    server_socket.listen(1)
    clientsock, clientAddress = server_socket.accept()
    newthread = ClientThread(clientAddress, clientsock)
    newthread.start()

server_socket.close()


