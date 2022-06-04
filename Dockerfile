FROM alpine

RUN echo "@testing http://nl.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories
RUN apk update

RUN apk add bash make python3 gcc g++ gfortran go nasm lua nodejs php openjdk18 mono@testing fpc@testing
