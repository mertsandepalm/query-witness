SELECT customer_id, SUM(amount_cents) AS total_cents
FROM payments
GROUP BY customer_id
HAVING SUM(amount_cents) > 10000;
