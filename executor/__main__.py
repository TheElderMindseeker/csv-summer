import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor

import aio_pika
import redis
import boto3


redis_db = redis.from_url(os.environ['EXECUTOR_REDIS_URL'], decode_responses=True)
boto_session = boto3.session.Session(
    aws_access_key_id=os.environ['EXECUTOR_AWS_ID'],
    aws_secret_access_key=os.environ['EXECUTOR_AWS_SECRET']
)
s3_storage = boto_session.client(
    service_name='s3',
    endpoint_url=os.environ['EXECUTOR_AWS_URL']
)


async def process_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
    async with message.process():
        task_id = int(message.body)
        task = json.loads(redis_db.get(f'task:{task_id}'))
        try:
            if task['storage'] == 'disk':
                file_obj = open(task['path'], 'rb')
            elif task['storage'] == 's3':
                file_obj = s3_storage.get_object(
                    Bucket=task['bucket'],
                    Key=task['key']
                )['Body'].iter_lines(chunk_size=2*1024*1024)  # Read in 2 MB chunks.
            else:
                raise ValueError(f"Unknown storage type {task['storage']}")
            io_loop = asyncio.get_event_loop()
            sums = await io_loop.run_in_executor(None, sum_every_10th_column, file_obj)
            redis_db.set(
                f'task:{task_id}:result',
                ','.join(str(value) for value in sums)
            )
            redis_db.set(f'task:{task_id}:status', 'done')
        except Exception as exc:
            print(exc)
            redis_db.set(f'task:{task_id}:status', 'failed')
        finally:
            redis_db.decr('task:active:counter')


def sum_every_10th_column(file_obj):
    header = next(file_obj).decode()
    print(header)
    # The strangest part of the task is the formatting of CSV-files. The attached CSV is
    # broken and full of double quotes. This script handles both correct and
    # attached-like CSV-files.
    if header.startswith('"') and header.strip().endswith('"'):
        # Remove newline, all double quotes and broken first comma, then split.
        columns = header.strip().replace('"', '')[1:].split(',')
        need_fixing = True
    else:
        columns = header.strip().split(',')
        need_fixing = False
    # Save index of every 10th column.
    indices = [idx for idx in range(len(columns)) if (idx + 1) % 10 == 0]
    sums = [0.0] * len(indices)
    # The files can be quite large, so it's better to stream them rather than reading
    # whole at once.
    for line in file_obj:
        line = line.decode()
        print(line)
        if need_fixing and len(line.strip().replace('"', '').replace(',', '')) == 0:
            continue
        if len(line.strip()) == 0:
            continue
        if need_fixing:
            columns = line.strip().replace('"', '').split(',')
        else:
            columns = line.strip().split(',')
        for s_idx, c_idx in enumerate(indices):
            sums[s_idx] += float(columns[c_idx])
    return sums


async def main() -> None:
    connection = await aio_pika.connect_robust(os.environ['EXECUTOR_RMQ_URL'])
    queue_name = os.environ['EXECUTOR_RMQ_QUEUE']
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.get_queue(queue_name)
    await queue.consume(process_message)
    try:
        await asyncio.Future()
    finally:
        await connection.close()


if __name__ == '__main__':
    io_loop = asyncio.get_event_loop()
    io_loop.set_default_executor(ThreadPoolExecutor(1))
    asyncio.run(main())
