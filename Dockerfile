FROM python:3.9-alpine

RUN pip install pipenv
RUN apk add git

WORKDIR /app

COPY Pipfile* ./

RUN PIP_USER=1 PIP_IGNORE_INSTALLED=1 pipenv install --system --deploy --ignore-pipfile

COPY auto_leave.py ./
COPY auto_leave.yml ./
COPY .env ./

ENTRYPOINT ["./auto_leave.py"]
