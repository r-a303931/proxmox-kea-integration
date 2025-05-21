FROM python:3.12

RUN apt update && apt install -y kea iproute2 && rm -rf /var/lib/apt/lists/* && mkdir /var/run/kea

WORKDIR /code

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY static/ static/
COPY app.py .

CMD [ "python", "app.py" ]
