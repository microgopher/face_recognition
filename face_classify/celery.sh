#!/bin/sh
celery -A face_app worker --loglevel=info
