# Source build (single-arch). CI releases use Dockerfile.release with
# cross-compiled binaries instead.
FROM rust:alpine AS build
ARG APP_VERSION
RUN apk add --no-cache musl-dev gcc
WORKDIR /app
COPY Cargo.toml Cargo.lock index.html ./
COPY src ./src
# APP_VERSION is baked into the binary at compile time (option_env!)
ENV APP_VERSION=$APP_VERSION
RUN cargo build --release

FROM scratch
COPY --from=build /app/target/release/fuel-host /fuel-host
EXPOSE 8000
ENTRYPOINT ["/fuel-host"]
