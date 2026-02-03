FROM python:3.9
WORKDIR /code
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "yuio.py"]