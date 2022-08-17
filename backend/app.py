import json
import os

import pika
import redis
from flask import Flask, abort, request
from pika.exceptions import AMQPError

app = Flask(__name__, static_folder=None)
redis_db = redis.from_url(os.environ['BACKEND_REDIS_URL'], decode_responses=True)


# Creating a connection each time we need to send a message is a bad thing. But for the
# sake of simplicity (RMQ connection requires async or multithread) and toy scale of the
# problem, we do it here.
def send_rmq_message(content: str) -> None:
    parameters = pika.URLParameters(os.environ['BACKEND_RMQ_URL'])
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.basic_publish(os.environ['BACKEND_RMQ_EXCHANGE'], '', content,
                          pika.BasicProperties(content_type='application/json'))
    connection.close()


@app.route('/tasks', methods=['POST'])
def new_task():
    if not request.is_json:
        abort(400)
    # Simple validation along with fields cherry picking.
    task = {
        'storage': request.json['storage']
    }
    if task['storage'] == 'disk':
        if 'path' not in request.json:
            abort(400)
        task['path'] = request.json['path']
    elif request.json['storage'] == 's3':
        if 'bucket' not in request.json or 'key' not in request.json:
            abort(400)
        task.update({
            'bucket': request.json['bucket'],
            'key': request.json['key']
        })
    else:
        abort(400)
    task_id = redis_db.incr('task:id:counter')
    status_key = f'task:{task_id}:status'
    task_key = f'task:{task_id}'
    redis_db.set(status_key, 'active')
    redis_db.set(task_key, json.dumps(task, indent=None, separators=(',', ':')))
    redis_db.incr('task:active:counter')
    try:
        send_rmq_message(str(task_id))
    except AMQPError:
        redis_db.decr('task:active:counter')
        redis_db.delete(status_key, task_key)
        raise
    return {'id': task_id}


@app.route('/active')
def active_tasks():
    active_tasks = redis_db.get('task:active:counter') or 0
    return {'tasks': int(active_tasks)}


@app.route('/tasks/<int:task_id>')
def task_info(task_id: int):
    status = redis_db.get(f'task:{task_id}:status')
    if status is None:
        abort(404)
    task = json.loads(redis_db.get(f'task:{task_id}'))
    response = {
        'status': status,
        'task': task
    }
    if status == 'done':
        response['result'] = redis_db.get(f'task:{task_id}:result')
    return response
