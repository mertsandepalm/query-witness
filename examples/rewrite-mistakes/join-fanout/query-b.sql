SELECT a.customer_id, a.product_id
FROM purchases AS a
JOIN purchases AS b ON a.customer_id = b.customer_id
WHERE a.product_id > 0;
