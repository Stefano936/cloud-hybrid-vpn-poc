\set ON_ERROR_STOP on
-- Ejecutar como postgres en una instancia dedicada al laboratorio.
SELECT 'CREATE ROLE poc_reader LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'poc_reader') \gexec
ALTER ROLE poc_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
SELECT 'CREATE DATABASE hybrid_poc'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'hybrid_poc') \gexec
\connect hybrid_poc
REVOKE ALL ON DATABASE hybrid_poc FROM PUBLIC;
GRANT CONNECT ON DATABASE hybrid_poc TO poc_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE TABLE IF NOT EXISTS public.products (
    id integer PRIMARY KEY,
    name text NOT NULL,
    price numeric(10,2) NOT NULL CHECK (price >= 0),
    stock integer NOT NULL CHECK (stock >= 0)
);
INSERT INTO public.products (id, name, price, stock)
SELECT n, 'Producto ficticio ' || n, (10 + n * 1.25)::numeric(10,2), 100 + n
FROM generate_series(1, 100) AS n
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, price = EXCLUDED.price,
    stock = EXCLUDED.stock;
GRANT USAGE ON SCHEMA public TO poc_reader;
REVOKE ALL ON public.products FROM poc_reader;
GRANT SELECT ON public.products TO poc_reader;
-- Establecer el secreto con \password poc_reader en psql, fuera de este archivo.
