FROM python:3.9-alpine

WORKDIR /app

COPY main.py config.py requirements.txt /app/

RUN pip install -r requirements.txt

CMD ["python", "main.py"]