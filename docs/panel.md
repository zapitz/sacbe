# Panel

Hay dos paneles con la misma página, armada en el servidor, sin JavaScript.

| | Panel interno | Panel con login |
|---|---|---|
| Contenedor | `pasarela-ia-panel` | `pasarela-ia-sacbe` |
| Dónde | Solo en `PIA_IP_PANEL` (127.0.0.1 o una IP privada) | Detrás de Cloudflare Access, en `PIA_SACBE_HOST` |
| Quién entra | Quien llegue a esa IP, siempre como dueño | Correos dados de alta en la tabla `usuario` |
| Cambios | Ninguno: solo lectura | Según el papel |

El panel interno dice arriba a dónde ir para hacer cambios (`https://<PIA_SACBE_HOST>`). Por qué no tiene formularios: [seguridad.md](seguridad.md).

## Qué muestra

- **Concesiones vigentes:** lo que queda de cada concesión, cuándo vence, la organización en uso y, al ritmo de gasto de los últimos 7 días, cuánto se perdería al vencer. Verde si se usa todo, ámbar si se pierde algo, rojo si se pierden más de US$20 o faltan menos de 3 días con saldo, gris si la organización nunca se ha cuadrado con Anthropic.
- **Programas:** gasto del mes de cada programa y su tope.
- **Pagado con tarjeta** este mes, contra el presupuesto, en dólares y en pesos.
- **Próximos vencimientos** y **salud del gestor:** si la API contesta, si la base responde, qué tan fresco está el cálculo de saldos y el catálogo de modelos.
- **Usuarios**, solo para el dueño.
- **Costos** (`/panel/costo`): costo total, peticiones y tokens; barras por día apiladas por modelo, programa u organización, con el detalle de cada día al pasar el cursor; leyenda con montos y tabla. Rango de 7, 30 o 90 días y filtro por programa u organización. Fechas en UTC, como la consola de Anthropic, para comparar cifra con cifra. Responsable y lector solo ven sus programas. Muestra solo lo que pasó por el gestor.

Sin formularios, la página se recarga sola cada 60 s. Con formularios no, para no borrar lo que estás escribiendo.

## Papeles

| Papel | Ve | Puede |
|---|---|---|
| `dueno` | Todo, la huella de cada llave (últimos 4 caracteres) y la lista de usuarios. | Dar de alta, rotar y dar de baja programas, cambiarles la clase, poner y quitar topes, y dar de alta, de baja, cambiar de papel, asignar y quitar proyectos a los usuarios. |
| `finanzas` | Todo menos huellas y usuarios: concesiones, programas, pagado, próximos vencimientos y salud. | Nada. |
| `responsable` | Solo el gasto de sus proyectos asignados, con la huella de su llave. | Rotar la llave de sus proyectos. |
| `lector` | Solo el gasto de sus proyectos asignados. | Nada. |

Siempre queda al menos un dueño activo: el gestor no deja dar de baja ni cambiar de papel al último.

Los usuarios se manejan desde el panel (dueño) o con `ops/pia usuario alta|baja|papel|asignar|quitar|lista`. Un usuario dado de baja pierde sus proyectos asignados; si vuelve, hay que asignárselos de nuevo.

Desde el panel no se dan de alta organizaciones ni concesiones de crédito, ni se cambian presupuestos o lecturas: eso es con `ops/pia`.

## Cómo funciona un cambio

1. El formulario lleva una ficha: un HMAC del correo y el día, firmado con `PIA_PANEL_SECRETO`. Vale el día en que se generó y el siguiente (UTC). Sin `PIA_PANEL_SECRETO` no hay formularios.
2. El gestor revisa, en orden: el JWT de Access y el usuario activo, que el `Origin` sea el del panel, el tamaño del formulario (16 KB), la ficha, la acción y el papel.
3. Rotar, dar de baja, quitar un tope, dar de baja un usuario y quitarle un proyecto piden marcar «confirmo».
4. El cambio queda en la bitácora (`ops/pia cambios`) con el correo de quien lo hizo y los campos del formulario.
5. Una llave nueva (alta o rotación) se ve una sola vez: el gestor redirige a `/panel/llave/<token>`, que solo abre quien hizo el cambio, durante 5 minutos. Después da 404. Si no la copiaste, rota otra vez.

Los cambios llegan al gestor en menos de un minuto: la API relee los proyectos cada `PIA_REFRESCO_S` segundos.
