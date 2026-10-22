FROM node:20-alpine

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY package*.json ./
RUN npm install --omit=dev --no-audit

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["node", "index.js"]
