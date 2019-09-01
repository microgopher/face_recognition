import copy
import datetime
import numpy as np

from pymongo import MongoClient
from bson.objectid import ObjectId

client = MongoClient()
db = client.devices


def process_face_duplicate():
    current_time = datetime.datetime.utcnow()
    hour_from_now = current_time - datetime.timedelta(hours=9)
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


if __name__ == '__main__':
    process_face_duplicate()
