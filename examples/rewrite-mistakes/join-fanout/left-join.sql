SELECT a.customer_id, a.product_id
FROM purchases AS a
LEFT JOIN purchases AS b
  ON a.customer_id = b.customer_id AND b.product_id = 2
WHERE a.product_id = 1;
