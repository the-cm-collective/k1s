FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/
COPY index.html /app/
COPY logo.svg /app/
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
