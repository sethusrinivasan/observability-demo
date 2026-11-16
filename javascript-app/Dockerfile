FROM node:20-slim

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY app.js ./

EXPOSE 8082

CMD ["npm", "start"]
