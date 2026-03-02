FROM python:3.12-slim
LABEL authors="yhma <yhma@sudytech.cn>"

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata ca-certificates && \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先创建用户
RUN useradd -m appuser

COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install .

COPY app/ ./app/
COPY main.py ./
RUN chown -R appuser /app

USER appuser

EXPOSE 8000
CMD ["python", "main.py"]