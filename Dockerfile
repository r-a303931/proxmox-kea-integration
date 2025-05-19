FROM python:3.12

WORKDIR /code

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY static/ static/
COPY app.py .

CMD [ "python", "app.py" ]
