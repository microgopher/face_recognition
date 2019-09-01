import copy
import config
from celery import Celery
import logging
import numpy as np

import requests
import time
import json
from flask import Flask, jsonify, request, send_file, abort
import io
from celery.task.schedules import crontab
from celery.decorators import periodic_task
import datetime
from pymongo import MongoClient
from bson.objectid import ObjectId
import celeryconfig

client = MongoClient()
db = client.devices

URL = "http://localhost:8009/"

from websocket import create_connection


def make_celery(app):
    celery = Celery(app.import_name, broker=config.CELERY_BROKER)
    celery.config_from_object(celeryconfig)
    celery.conf.update(app.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask

    return celery

face = Flask(__name__)

#face.register_blueprint(api)

face.config.from_object('config')
celery = make_celery(face)

@face.route('/add', methods=['post'])
def route_face_add():
    data = request.json
    process_face_add(data)
    #process_face_add.apply_async((data,))
    return jsonify({'message': 'Image was register succesfully, Result will be return soon.'})


@face.route('/callback', methods=['post'])
def route_callback():
    payload = request.json
    face_id = payload['image_id']
    face_record = db.faces.find_one({'_id': ObjectId(face_id)})
    if not face_record:
       abort(404)
    update = True
    if face_record and 'error' in payload:
       print (payload['error'])
       face_record['is_not_face'] = True
    
    if face_record and 'result_age' in payload:
        face_record['age'] = payload['result_age']
    if face_record and 'result_gender' in payload:
        face_record['gender'] = payload['result_gender']
    if face_record and 'result_align' in payload:
        update = False
        face_record['align'] = payload['result_align']
    db.faces.save(face_record)
    if update:
        ws = create_connection("ws://localhost:8765/pub")
        ws.send(json.dumps({'topic': 'update', 'face_id': face_id, 
               'age': payload.get('result_age'),
               'gender': payload.get('result_gender'),
               'error': payload.get('error')
        }))
        ws.close()
    return jsonify({'message': 'sucess'})

@face.route('/face_image/<face_id>')
def route_face_image(face_id):
    face_record = db.faces.find_one({'_id': ObjectId(face_id)})
    if face_record:
        face_path = face_record['face_path']
        with open(face_path, 'rb') as bites:
           return send_file(
                     io.BytesIO(bites.read()),
                     attachment_filename=face_id + '.jpeg',
                     mimetype='image/jpg'
               )
    else:
        abort(404)

@celery.task(name='process_face_add')
def process_face_add(data):
    data['age'] = False
    data['gender'] = False
    data['align'] = False
    data['parent'] = False
    data['is_not_face'] = False
    data['create_date'] = datetime.datetime.utcnow()
    res = db.faces.insert_one(data)
    payload = {
      'image_id': str(res.inserted_id),
      'image_path': data['face_path'],
      'callback_url': request.url_root + 'callback'
    }
    #send request for gender classification
    requests.post(URL + 'face_gender', json=payload)
    #send request for age classification
    requests.post(URL + 'face_age', json=payload)
    #send request for align classification
    requests.post(URL + 'face_align', json=payload)

    
    ws = create_connection("ws://localhost:8765/pub")
    ws.send(json.dumps({'topic': 'new','face_id': str(res.inserted_id), 'face_url': request.url_root + 'face_image/' + str(res.inserted_id)}))
    ws.close()




@celery.task(name='periodic_face_duplicate')
def process_face_duplicate():
    current_time = datetime.datetime.utcnow()
    print ("process face duplicate is started...")
    hour_from_now = current_time - datetime.timedelta(hours=1)
    face_records = db.faces.find({'create_date': {'$lt': current_time, '$gte': hour_from_now}, 'parent': False, 'is_not_face': False})
    records = list(face_records)
    childs = []
    for record in records:
        parent_align = record['align']
        if not parent_align:
            continue

        record_id = record['_id']

        if record_id in childs:
           continue

        todo_records = copy.copy(records)

        for child in todo_records:

            if child['_id'] == record_id:
                continue

            child_align = child['align']

            dist = np.sqrt(np.sum(np.square(np.subtract(parent_align, child_align))))

            if dist < 1.0:
                print (record['face_path'], child['face_path'], dist)
                face_record = db.faces.find_one({'_id': ObjectId(child['_id'])})
                face_record['parent'] = record_id
                childs.append(child['_id'])
                db.faces.save(face_record)
                ws = create_connection("ws://localhost:8765/pub")
                ws.send(json.dumps({'topic': 'update','face_id': str(child['_id']), 'parent_url': request.url_root + 'face_image/' + str(record_id), 'parent': str(record_id)}))
                ws.close()


if __name__ == '__main__':
    print ("I am Started...")
    face.run(debug=True, port=8010)
