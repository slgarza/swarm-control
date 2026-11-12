# Empezar con Swarm Control

Swarm Control es el programa que coordina agentes para trabajar en un repositorio.
Lo instalas una vez en tu computadora y lo manejas desde un panel en el navegador.
El proyecto que quieres modificar, por ejemplo Afterimage, se conserva en otra
carpeta y con su propia configuración.

## 1. Descargar y abrir el panel

Necesitas Git y Python 3.12 o superior, con soporte para entornos virtuales.
Usa una terminal en Linux o macOS. En Windows, usa una terminal de WSL; los
comandos siguientes no son para PowerShell. Si todavía no tienes esos programas,
instálalos antes de continuar.

Abre la terminal en la carpeta donde quieres guardar el controlador y ejecuta
una línea a la vez:

```sh
git clone https://github.com/slgarza/swarm-control.git
cd swarm-control
./easy-run
```

La primera vez, Easy Run crea `.venv`, instala las dependencias y abre el panel.
El proceso puede tardar mientras descarga paquetes. Después verás una dirección
como `http://127.0.0.1:8787`. Si no se abre el navegador, copia esa dirección en él.
No necesitas activar el entorno de Python para usar Easy Run.

Mantén esa terminal abierta mientras usas el panel. Para apagar el servidor,
presiona **Ctrl+C** en la terminal cuando no haya una ejecución en curso. La
próxima vez, entra a la misma carpeta `swarm-control` y ejecuta `./easy-run`.
Tus repositorios registrados y su historial se conservan.

Si descargaste un ZIP, extráelo primero y abre la terminal dentro de la carpeta
que contiene `easy-run` y `pyproject.toml`. Si `./easy-run` indica “Permission
denied”, ejecuta `chmod +x easy-run` una vez y vuelve a intentar. También puedes
usar `sh easy-run` con las mismas opciones en los siguientes pasos. El controlador
puede abrirse desde ese ZIP, pero un proyecto de trabajo sí necesita ser un
repositorio Git.

## 2. Probar primero con el ejemplo local

Puedes conocer el flujo sin claves, cuentas de servicios ni gasto en agentes.
Detén el servidor con Ctrl+C y, desde la carpeta `swarm-control`, ejecuta:

```sh
./.venv/bin/python examples/create_demo.py ../swarm-demo
./easy-run --project ../swarm-demo
```

El primer comando crea otra carpeta, `swarm-demo`, con un repositorio Git y dos
tareas. No crea un repositorio en GitHub. Si esa carpeta ya existe, elige otro
nombre y úsalo en ambos comandos; el ejemplo se niega a sobrescribirla.
El segundo comando registra el ejemplo y vuelve a abrir el panel.

En **Repository**, selecciona el ejemplo; después:

1. Pulsa **New run**.
2. Selecciona las tareas **D01** y **D02**.
3. Pon **Concurrent agents** en **2**.
4. Pulsa **Check plan** y revisa el resultado.
5. Pulsa **Save draft** y abre el borrador guardado en **Run history**.

Llegaste al resultado esperado: un plan guardado con dos tareas. El ejemplo
tiene las ejecuciones deshabilitadas y presupuesto cero. Todavía no trabajó
ningún agente y no se creó ninguna máquina en la nube.

## 3. Conectar tu propio proyecto

Primero descarga tu proyecto con Git. Por ejemplo, para un repositorio público,
reemplaza `TU_USUARIO/TU_REPO` y ejecuta esto desde la carpeta donde guardas tus
proyectos:

```sh
git clone https://github.com/TU_USUARIO/TU_REPO.git
```

En el panel, pulsa **Connect repository**. Escribe la ruta completa a la carpeta
local de ese proyecto o a su archivo `swarm.toml`; si la ruta contiene espacios,
consérvalos sin agregar comillas dentro del campo. Puedes obtener la ruta con
`pwd` desde la terminal del proyecto. La ruta corresponde a la computadora que
ejecuta Easy Run. Una URL de GitHub no sirve en este campo.
En WSL, usa la ruta que devuelve `pwd`, por ejemplo `/mnt/c/Users/TuUsuario/mi-repo`,
en lugar de una ruta de Windows como `C:\Users\TuUsuario\mi-repo`.

También puedes conectarlo al abrir el panel:

```sh
./easy-run --project "/ruta/completa/a/mi proyecto"
```

El proyecto necesita estar preparado: `swarm.toml` en su raíz Git, tareas válidas
en la carpeta configurada, los documentos de instrucciones y comandos de
validación. El repositorio configurado debe coincidir con su remoto `origin`.
El panel verifica estos datos antes de registrarlo. Conectar otra vez la misma
carpeta conserva su historial.

Si tu proyecto no tiene esos archivos, sigue la
[referencia de configuración](PROJECTS.md) para prepararlos. No basta con copiar
la configuración de otro proyecto: los comandos, tareas, rutas de trabajo y
servicios deben corresponder al tuyo. Usa el ejemplo local mientras preparas
esa configuración. El controlador no convierte cualquier repositorio en un
backlog de tareas automáticamente.

Para cambiar de proyecto, usa **Repository**. **Saved run** elige una ejecución o
borrador dentro de ese proyecto. Cada proyecto conserva su propio historial.

## 4. Preparar un run

Un *run* es una selección guardada de tareas, agentes, código de partida y límites.
En **New run**, selecciona tareas reales del repositorio y el número de
**Concurrent agents**. Empieza con una o dos tareas independientes.

El número de agentes determina cuántas tareas pueden trabajarse a la vez. Con
dos tareas y dos agentes, cada agente recibe una tarea. Con más tareas que
agentes, las tareas adicionales esperan un turno.

**Total model reservation · USD** es el límite propuesto para el modelo durante
todo ese run. No es un precio por agente ni el saldo de tu cuenta. La reserva
para infraestructura se revisa por separado. **Check plan** muestra bloqueos y
**Save draft** guarda la selección exacta. Ninguno inicia agentes.

Si cambias el código o la configuración después de guardar, crea y revisa otro
borrador. Un borrador anterior no se actualiza silenciosamente.

## 5. Ejecutar agentes de verdad

Antes de una corrida grande, pulsa **Check plan** y revisa **Parallelism review**.
Cien tareas en una cadena de dependencias permiten comenzar una sola; cien tareas
independientes pueden ocupar cien agentes. El panel muestra la frontera lista,
la cadena más larga y las etapas teóricas. Al elegir 100 agentes propone exigir
100 tareas independientes antes del lanzamiento. Las etapas posteriores necesitan
dependencias integradas y otro borrador sobre la nueva base.

El detalle **Context and acceptance readiness** indica si existen extractos de
contrato y pruebas de aceptación por tarea. La supervisión registra cambios de
archivos y el motivo observado de parada. **Stop after no source changes** es
opcional: vacío conserva avisos y el límite total, sin detener por silencio del
modelo. Consulta [dependencias](DEPENDENCIES.md) y
[supervisión](WORKER_SUPERVISION.md) para preparar los contratos.

Esta parte usa servicios de pago. Antes necesitas preparar tus propias cuentas
de GitHub, Modal y OpenAI, configurar el presupuesto autorizado, publicar las
tareas y el código, desplegar tu gateway y verificar su funcionamiento. Sigue
el [tutorial de dos agentes](TWO_AGENT_WALKTHROUGH.md), que contiene los pasos y
comandos. Abrir Easy Run no hace esa configuración ni concede presupuesto.

Cuando esté preparado, abre el borrador en el panel, revisa las tareas y los
límites, introduce el origen HTTPS de tu gateway verificado y marca la
confirmación de autorización. Usa la URL `https://NOMBRE.modal.run` que devuelve
tu despliegue, sin agregar `/v1`, otras rutas, puertos ni claves. Pulsa **Run agents**.
El servidor aplica las mismas
validaciones que la CLI y muestra el estado de la solicitud. Conserva abierta
la terminal del servidor durante toda la ejecución.

Si se corta la conexión o queda un resultado incierto, revisa **Run history**
antes de hacer otra acción. No repitas una ejecución para “ver si ahora sí”:
puede haber reservas o trabajo ya iniciado. Se conserva cada intento.

Un agente puede devolver un parche validado. Después todavía hay que revisar
el código, aprobar sus pruebas e integrar el PR. **Runner finished** indica que
terminó el proceso; no significa que el cambio ya esté aceptado o integrado.

## Problemas comunes

| Lo que ves | Qué hacer |
| --- | --- |
| No encuentra Python o la versión es antigua | Instala Python 3.12 o superior y vuelve a ejecutar Easy Run. |
| Falta `venv` o `ensurepip` | Instala el paquete de soporte para entornos virtuales de tu Python; consulta [solución de problemas](TROUBLESHOOTING.md). |
| El puerto 8787 está ocupado | Ejecuta `./easy-run --port 8788` y abre la nueva dirección. |
| El navegador no se abre | Copia la URL de la terminal; `./easy-run --no-open` también la muestra. |
| No aparece tu repositorio | Conéctalo con su ruta local; comprueba que estás usando el mismo workspace. |
| No encuentra `swarm.toml` | Estás eligiendo otra carpeta o el proyecto todavía necesita [configuración](PROJECTS.md). |
| No hay tareas seleccionables | Revisa los archivos de tareas y su estado en el proyecto; usa el demo para comprobar el panel. |
| No permite ejecutar | Lee los bloqueos. Guardar un plan no configura cuentas, habilita ejecución ni autoriza gasto. |
| `swarm-control` no se encuentra en otra terminal | Usa `./easy-run`, o activa el entorno con `source .venv/bin/activate` desde la carpeta del controlador. |

Para detalles de opciones e historial, consulta [repositorios y runs](RUNS.md).
