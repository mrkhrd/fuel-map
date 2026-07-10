FROM rust:alpine AS build
ARG APP_VERSION
RUN apk add --no-cache musl-dev pkgconf openssl-dev openssl-libs-static
WORKDIR /app
COPY Cargo.toml Cargo.lock index.html ./
COPY src ./src
# APP_VERSION is baked into the binary at compile time (option_env!)
ENV OPENSSL_STATIC=1 APP_VERSION=$APP_VERSION
RUN cargo build --release

FROM alpine:3
RUN apk add --no-cache ca-certificates
COPY --from=build /app/target/release/fuel-host /usr/local/bin/fuel-host
EXPOSE 8000
ENTRYPOINT ["fuel-host"]
