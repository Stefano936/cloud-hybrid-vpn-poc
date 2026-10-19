# Validación de implementación

Comprobaciones realizadas el 19 de octubre de 2026. **No son resultados del experimento EC2–entorno local** y no sustituyen la corrida que debe grabar el equipo.

## Entorno de comprobación

Ubuntu 24.04.4 en WSL2, kernel 6.6.87.2, arquitectura amd64, Python 3.12.3, PostgreSQL 16 y k6 1.0.0. Se instalaron las versiones fijadas en los archivos de dependencias; `pip check` no encontró incompatibilidades.

## Comprobaciones automatizadas incluidas

`python -m pytest -q` terminó con **8 pruebas aprobadas**, sin advertencias, y verifica:

- Salud independiente de PostgreSQL, límites del parámetro y rechazo de entradas inválidas.
- Error 503 controlado seguido de recuperación, sin filtrar el error interno ni cachear respuestas.
- Tiempo total acotado durante la conexión y la consulta, cierre de la conexión y salud accesible mientras una consulta espera.
- Separación entre latencias exitosas y fallas, recuperaciones distintas, N/A cuando no se observó recuperación, advertencias por iteraciones descartadas, p95 con suficientes muestras y rechazo de corridas incompletas o saltos de reloj.
- Generación de CSV, Markdown y PNG sobre datos explícitamente sintéticos.

También se comprobaron importaciones, compilación Python, sintaxis Bash del instalador y las dos gráficas generadas mediante inspección visual.

## Comprobación de integración realizada

Se crearon dos namespaces de red Linux aislados, conectados por un enlace virtual y un túnel **WireGuard real**. Dentro de ellos se ejecutaron PostgreSQL y FastAPI, con k6 junto a la API. Esto ejercita los protocolos y scripts reales en un solo host; no reproduce Internet, EC2, NAT doméstico ni dos kernels independientes.

Se verificó:

1. Inicialización SQL dos veces, con 100 productos, sin duplicación.
2. Autenticación SCRAM con el HBA suministrado; el rol lector no pudo borrar datos.
3. Lectura de PostgreSQL real por el túnel y visibilidad inmediata de una modificación de un producto, luego revertida.
4. Aplicación de 100 ms de netem y aumento observado de duración de consultas.
5. Retiro del retardo antes del corte; durante el corte, respuesta 503 acotada y `/healthz` disponible.
6. Restauración de productos sin reiniciar la API y limpieza repetible.
7. Conservación de un qdisc ajeno y una regla de firewall ajena; rechazo de un retardo inválido.
8. Corrida continua de cuatro etapas de aproximadamente 8/8/6/8 s, con 1 solicitud/s: k6 finalizó naturalmente con código 0, 31 consultas completas y **0 iteraciones descartadas**. Se registraron fallas y luego recuperación tanto ICMP como de productos. El analizador generó las dos gráficas y ambos cuadros.
9. Estado final sin netem ni reglas DROP del proyecto; eliminación de los namespaces de prueba.

Durante esta comprobación aislada se lanzó el orquestador desde un proceso privilegiado y se omitió únicamente su comprobación de UID de operador. Los comandos de red, k6, la API y el procesamiento fueron los del repositorio. La política sudo de la EC2 final debe verificarse allí. Los archivos temporales de esta comprobación no se incorporan a `results/` ni se presentan como resultados del proyecto.

## Pendiente en el laboratorio del equipo

- Crear/configurar manualmente EC2 y la VM Linux local, instalar dependencias y generar sus propias claves y contraseña.
- Validar Security Group, firewalls, NAT, UDP y rutas con las IP reales. Confirmar que PostgreSQL no escucha en interfaces públicas y que sólo admite el origen previsto.
- Comprobar en esa EC2 el módulo netem y que `sudo -v` permite terminar toda la corrida y su limpieza.
- Ejecutar la secuencia predeterminada de unos 105 segundos. Conservar crudos, configuración, figuras y video. No usar los ejemplos sintéticos como evidencia.
- Revisar las recuperaciones, solicitudes que atraviesan fronteras y variabilidad; repetir si hay iteraciones descartadas o cambios de reloj.
- Grabar siguiendo `DEMO.md` y formular conclusiones limitadas al laboratorio observado.

No se accedió a AWS ni se crearon recursos pagos para estas comprobaciones.
