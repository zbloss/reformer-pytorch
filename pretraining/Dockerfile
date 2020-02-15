FROM deepspeed/deepspeed:latest

#COPY requirements.txt /

#RUN pip install -r requirements.txt

WORKDIR /pretraining

CMD ["deepspeed", "deepspeed_self-supervised.py", "--deepspeed", "--deepspeed_config", "deepspeed_config.json"]