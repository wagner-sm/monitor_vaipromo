import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
import os
import urllib.request
from urllib.error import HTTPError
import html

# =======================
# LOGGING
# =======================
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')


class VaiPromoMonitor:
    URL = "https://www.vaidepromo.com.br/passagens-aereas/"

    def __init__(self):
        self.config = self.carregar_config()
        self.resultados = []

    # =======================
    # CONFIG
    # =======================
    def carregar_config(self):
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        logging.info(f"Configuração carregada: {len(config['CONSULTAS'])} consultas")
        return config

    # =======================
    # HELPERS
    # =======================
    def trigger_change(self, page, selector):
        page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (el) {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }
            }""",
            selector
        )

    def preencher_localizacao(self, page, campo, sigla):
        el = page.locator(f'[data-cy="{campo}"]')
        el.click()
        el.fill(sigla)
        page.wait_for_selector(f'[role="option"]:has-text("{sigla}")', timeout=5000)
        page.locator(f'[role="option"]:has-text("{sigla}")').first.click()
        self.trigger_change(page, f'[data-cy="{campo}"]')

    def clicar_como_humano(self, locator, page):
        locator.scroll_into_view_if_needed()
        locator.hover()
        page.wait_for_timeout(150)
        locator.click()
        page.wait_for_timeout(300)

    # =======================
    # CALENDÁRIO
    # =======================
    def selecionar_data_como_humano(self, page, data_str):
        data = datetime.strptime(data_str, "%d/%m/%Y")
        data_cy = data.strftime("%d-%m-%Y")

        seletor_dia = f'button[data-cy="{data_cy}"]'
        seletor_next = 'button[data-cy="data-range-picker-next"]'

        page.wait_for_selector(seletor_next)

        for _ in range(24):  # até 24 meses
            dia = page.locator(seletor_dia)
            if dia.count() > 0:
                self.clicar_como_humano(dia.first, page)

                # sincroniza React
                page.evaluate(
                    """(date) => {
                        const input = document.querySelector('[data-cy="departure-date"] input');
                        if (input) {
                            input.value = date;
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                            input.dispatchEvent(new Event('blur', { bubbles: true }));
                        }
                    }""",
                    data_str
                )
                return

            page.locator(seletor_next).first.click()
            page.wait_for_timeout(600)

        raise Exception("Data não encontrada no calendário")

    # =======================
    # RESULTADOS
    # =======================
    def wait_for_results(self, page, timeout=30):
        start = time.time()
        last = stable = 0

        while time.time() - start < timeout:
            count = page.locator('div[class*="_content_"]').count()
            if count == last and count > 0:
                stable += 1
                if stable >= 3:
                    return
            else:
                stable = 0
            last = count
            time.sleep(1)

    # =======================
    # EXTRAÇÃO
    # =======================
    def extrair_voos(self, page):
        try:
            return page.evaluate(
                """() => {
                    const cards = document.querySelectorAll('div[class*="_content_"]');
                    const voos = [];

                    cards.forEach(card => {
                        const prices = [...card.querySelectorAll('strong')]
                            .map(s => s.textContent.trim())
                            .filter(t => t.includes('R$'))
                            .map(t => ({
                                text: t.replace(/\\u00a0/g,' '),
                                value: parseFloat(
                                    t.replace(/[^0-9,]/g,'')
                                     .replace('.', '')
                                     .replace(',', '.')
                                )
                            }))
                            .filter(p => !isNaN(p.value));

                        if (!prices.length) return;
 
                        const final = prices.reduce((a,b) => a.value > b.value ? a : b);

                        const airline =
                            card.querySelector('img[alt]')?.alt ||
                            card.querySelector('span[class*="iata"]')?.textContent?.trim() ||
                            "Companhia não identificada";

                        voos.push({
                            companhia: airline,
                            preco: final.text,
                            valor: final.value
                        });
                    });

                    const unique = {};
                    voos.forEach(v => unique[v.companhia + v.valor] ||= v);

                    return Object.values(unique).sort((a,b) => a.valor - b.valor);
                }"""
            )
        except Exception as e:
            logging.error(f"Erro ao extrair voos: {e}")
            return []

    # =======================
    # CONSULTA
    # =======================
    def executar_consulta(self, consulta):
        resultado = {
            "consulta": consulta,
            "timestamp": datetime.now().isoformat(),
            "voos": []
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(self.URL, timeout=60000)

                page.get_by_role("button", name="Só ida ou volta").click()
                self.preencher_localizacao(page, "departure", consulta["origem"])
                self.preencher_localizacao(page, "arrival", consulta["destino"])

                page.get_by_role("textbox", name="Ida").nth(1).click()
                self.selecionar_data_como_humano(page, consulta["data"])

                page.evaluate(
                    """() => {
                        const form = document.querySelector('form');
                        form && form.dispatchEvent(new Event('submit', { bubbles: true }));
                    }"""
                )

                page.wait_for_function(
                    "() => location.href.includes('search') || document.querySelectorAll('div[class*=\"_content_\"]').length > 0",
                    timeout=60000
                )

                for _ in range(4):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)

                self.wait_for_results(page)
                resultado["voos"] = self.extrair_voos(page)
                resultado["url"] = page.url

                browser.close()

        except Exception as e:
            resultado["error"] = str(e)
            logging.error(f"Erro na consulta: {e}")

        return resultado

    # =======================
    # TELEGRAM
    # =======================
    def enviar_telegram(self, texto):
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        message_id = os.getenv("TELEGRAM_MESSAGE_ID")

        if not token or not chat_id:
            logging.warning("Telegram não configurado")
            return

        def req(method, payload):
            url = f"https://api.telegram.org/bot{token}/{method}"
            data = json.dumps(payload).encode("utf-8")
            try:
                return json.loads(
                    urllib.request.urlopen(
                        urllib.request.Request(
                            url,
                            data=data,
                            headers={"Content-Type": "application/json"}
                        ),
                        timeout=10
                    ).read()
                )
            except HTTPError as e:
                # 🔑 comportamento antigo: falha silenciosa
                try:
                    return {"ok": False, "error": e.read().decode()}
                except Exception:
                    return {"ok": False, "error": str(e)}

        payload = {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        # tenta editar mensagem existente
        if message_id:
            payload_edit = {**payload, "message_id": int(message_id)}
            r = req("editMessageText", payload_edit)
            if r.get("ok"):
                logging.info("✏️ Mensagem editada com sucesso")
                return
            else:
                logging.warning("⚠️ Não foi possível editar mensagem, enviando nova")

        # envia nova mensagem
        r = req("sendMessage", payload)
        if r.get("ok"):
            novo_id = r["result"]["message_id"]
            logging.info(f"✅ Nova mensagem enviada ({novo_id})")
            logging.info(f"💡 TELEGRAM_MESSAGE_ID={novo_id}")
        else:
            logging.error(f"❌ Falha ao enviar mensagem: {r}")


    def resumo_telegram(self):
        agora = datetime.now(ZoneInfo("America/Sao_Paulo"))

        linhas = [
            "✈️ <b>VaiPromo Monitor</b>",
            f"🕐 <i>Atualizado em {agora:%d/%m/%Y às %H:%M}</i>"
        ]

        for r in self.resultados:
            c = r["consulta"]
            linhas.append(
                f"\n<b>{html.escape(c['origem'])} → {html.escape(c['destino'])} "
                f"({html.escape(c['data'])})</b>"
            )

            if "error" in r:
                linhas.append(f"❌ {html.escape(r['error'])}")
                continue

            for i, v in enumerate(r["voos"][:3]):
                companhia = html.escape(v["companhia"])
                preco = html.escape(v["preco"])

                if i == 0:
                    linhas.append(f"💰 <b>{companhia}</b> – {preco}")
                else:
                    linhas.append(f"#{i+1} {companhia} – {preco}")

            if "url" in r:
                url = html.escape(r["url"])
                linhas.append(f'🔗 <a href="{url}">Ver no VaiPromo</a>')

        return "\n".join(linhas)


    # =======================
    # EXECUÇÃO
    # =======================
    def executar(self):
        for consulta in self.config["CONSULTAS"]:
            logging.info(f"{consulta['origem']} → {consulta['destino']} - {consulta['data']}")
            self.resultados.append(self.executar_consulta(consulta))

        self.enviar_telegram(self.resumo_telegram())
        logging.info("✅ Execução concluída!")


def main():
    VaiPromoMonitor().executar()


if __name__ == "__main__":
    main()