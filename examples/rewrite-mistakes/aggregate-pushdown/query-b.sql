SELECT customer_id, SUM(amount_cents) AS total_cents
FROM payments
WHERE amount_cents > 10000
GROUP BY customer_id
HAVING SUM(amount_cents) > 10000;
