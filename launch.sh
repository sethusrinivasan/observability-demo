docker-compose up -d


docker build -t observability-demo .
docker run --network="host" observability-demo



