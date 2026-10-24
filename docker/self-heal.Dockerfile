FROM python:3.11-slim

RUN pip install --no-cache-dir python-docx==1.2.0 lxml==6.1.3

WORKDIR /code
