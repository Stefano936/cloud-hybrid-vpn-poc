# Datos sintéticos

`python scripts/make_synthetic.py` genera archivos deterministas en `output/`,
separados de `results/` e ignorados por Git. No son mediciones ni valores esperados.

`python scripts/analyze.py examples/synthetic/output` comprueba tablas y figuras.
Las figuras y el informe llevan una marca explícita de datos sintéticos.
