FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY HSK1_Vocabulary.xlsx .
COPY web.py .
COPY static/ static/

CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
