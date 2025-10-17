
FROM python:3.12
LABEL authors="odin"


RUN mkdir /usr/src/namazu


COPY . /usr/src/namazu


COPY requirements.txt /usr/src/namazu


WORKDIR /usr/src/namazu


RUN pip install -r requirements.txt


CMD ["python", "main.py"]
