# Basic health check
curl http://localhost:5000/

# Generate compute requests
for i in {1..20}; do
  curl http://localhost:5000/compute/$((RANDOM % 15 + 5))
  echo ""
  sleep 0.5
done
