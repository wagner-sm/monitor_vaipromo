import json
import logging
import time
from datetime import datetime
from playwright.sync_api import sync_playwright
import os
import urllib.request

# =======================
# LOGGING
# =======================
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')


class VaiPromoMonitor:
    def __init__(self):
        self.config = self.carregar_config()
        self.resultados = []
        self.url = "https://www.vaidepromo.com.br/passagens-aereas/"
        self.tempo_espera = 8000

    # =======================
    # CONFIG
    # =======================
    def carregar_config(self):
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
        logging.info(f"Configuração carregada: {len(config['CONSULTAS'])} consultas")
        return config

    # =======================
    # ALERTA DE PREÇO
    # =======================
    def verificar_alerta_preco(self, resultado):
        """Verifica se o preço está abaixo do alerta configurado"""
        consulta = resultado["consulta"]
        
        if not resultado["voos"] or "preco_alerta" not in consulta:
            return None
        
        menor_preco = resultado["voos"][0]["valor"]
        preco_alerta = consulta["preco_alerta"]
        
        if menor_preco <= preco_alerta:
            return {
                "encontrado": True,
                "menor_preco": menor_preco,
                "preco_alerta": preco_alerta,
                "companhia": resultado["voos"][0]["companhia"]
            }
        
        return None

    # =======================
    # HELPERS
    # =======================
    def trigger_change(self, page, selector):
        page.evaluate(f"""
        () => {{
            const el = document.querySelector('{selector}');
            if (el) {{
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        }}
        """)

    def preencher_localizacao(self, page, campo, sigla):
        el = page.locator(f'[data-cy="{campo}"]')
        el.click()
        el.fill(sigla)
        page.wait_for_selector(f'[role="option"]:has-text("{sigla}")', timeout=5000)
        page.locator(f'[role="option"]:has-text("{sigla}")').first.click()
        self.trigger_change(page, f'[data-cy="{campo}"]')

    # =======================
    # CALENDÁRIO
    # =======================
    def navegar_para_data(self, page, data_str):
        data = datetime.strptime(data_str, "%d/%m/%Y")
        data_cy = data.strftime("%d-%m-%Y")
        seletor = f'button[data-cy="{data_cy}"]'

        for _ in range(12):
            if page.locator(seletor).count() > 0:
                page.locator(seletor).first.scroll_into_view_if_needed()
                page.locator(seletor).first.click(force=True)
                return

            page.locator('button[data-cy="data-range-picker-next"]').first.click()
            page.wait_for_timeout(400)

        raise Exception(f"Data {data_str} não encontrada no calendário")

    # =======================
    # RESULTADOS
    # =======================
    def wait_for_results(self, page, timeout=30):
        start = time.time()
        last = 0
        stable = 0

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
    # EXTRAÇÃO DE VOOS
    # =======================
    def extrair_voos(self, page):
        """Extrai voos da página com tratamento de erros"""
        try:
            voos = page.evaluate("""
            () => {
                const voos = [];

                document.querySelectorAll('div[class*="_content_"]').forEach(card => {
                    const prices = [];

                    card.querySelectorAll('strong').forEach(s => {
                        const t = s.textContent.trim();
                        if (t.includes('R$')) {
                            const v = parseFloat(
                                t.replace(/[^0-9,]/g,'')
                                .replace('.', '')
                                .replace(',', '.')
                            );
                            if (!isNaN(v) && v > 0) {
                                prices.push({ text: t.replace(/\\u00a0/g,' '), value: v });
                            }
                        }
                    });

                    if (!prices.length) return;

                    const finalPrice = prices.reduce((a,b) => a.value > b.value ? a : b);

                    let company = "Companhia não identificada";
                    const selectors = [
                        'div[class*="iata"] span',
                        'span[class*="iata"]',
                        'div[class*="airline"] span',
                        'img[alt]'
                    ];

                    for (const sel of selectors) {
                        const el = card.querySelector(sel);
                        if (el) {
                            company = el.textContent?.trim() || el.alt || company;
                            if (company && company !== "Companhia não identificada") break;
                        }
                    }

                    voos.push({
                        companhia: company,
                        preco: finalPrice.text,
                        valor: finalPrice.value
                    });
                });

                const unique = {};
                voos.forEach(v => {
                    const k = v.companhia + v.valor;
                    if (!unique[k]) unique[k] = v;
                });

                return Object.values(unique)
                    .sort((a,b) => a.valor - b.valor)
                    .map(v => ({ 
                        companhia: v.companhia, 
                        preco: v.preco,
                        valor: v.valor  // Adicionar valor também
                    }));
            }
            """)
            
            return voos if voos else []
            
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
            "voos": [],
            "alerta": None
        }

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(self.url, timeout=60000)

                page.get_by_role("button", name="Só ida ou volta").click()
                self.preencher_localizacao(page, "departure", consulta["origem"])
                self.preencher_localizacao(page, "arrival", consulta["destino"])

                page.get_by_role("textbox", name="Ida").nth(1).click()
                self.navegar_para_data(page, consulta["data"])

                page.get_by_role("button", name="Encontrar voos").click()
                page.wait_for_timeout(self.tempo_espera)

                for _ in range(4):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)

                self.wait_for_results(page)

                resultado["voos"] = self.extrair_voos(page)
                resultado["url"] = page.url
                
                # Verificar alerta de preço
                resultado["alerta"] = self.verificar_alerta_preco(resultado)

                browser.close()

        except Exception as e:
            resultado["error"] = str(e)
            logging.error(f"Erro na consulta: {e}")

        return resultado

    # =======================
    # TELEGRAM
    # =======================         
    def enviar_telegram(self, texto):
        """Envia ou edita mensagem no Telegram"""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        # NOVO: ID fixo da mensagem (você vai pegar esse ID na primeira execução)
        message_id = os.getenv("TELEGRAM_MESSAGE_ID")

        if not token or not chat_id:
            logging.warning("Telegram não configurado")
            return

        # Se já existe um message_id, EDITAR a mensagem existente
        if message_id:
            try:
                logging.info(f"🔄 Tentando editar mensagem {message_id}...")
                
                url = f"https://api.telegram.org/bot{token}/editMessageText"
                data = json.dumps({
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }).encode("utf-8")

                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                response = urllib.request.urlopen(req, timeout=10)
                result = json.loads(response.read())
                
                if result.get("ok"):
                    logging.info(f"✏️ Mensagem {message_id} editada com sucesso!")
                    return
                else:
                    logging.warning(f"⚠️ API retornou erro: {result}")
                
            except Exception as e:
                logging.warning(f"⚠️ Erro ao editar mensagem {message_id}: {e}")
                logging.info("📤 Enviando nova mensagem...")
        else:
            logging.info("ℹ️ TELEGRAM_MESSAGE_ID não configurado, enviando nova mensagem...")

        # Se não existe message_id ou falhou ao editar, CRIAR nova mensagem
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        response = urllib.request.urlopen(req, timeout=10)
        
        result = json.loads(response.read())
        if result.get("ok"):
            novo_message_id = result["result"]["message_id"]
            logging.info(f"📤 Nova mensagem enviada: {novo_message_id}")
            logging.info(f"⚙️ Adicione esta variável no GitHub Actions:")
            logging.info(f"   TELEGRAM_MESSAGE_ID={novo_message_id}")
        else:
            logging.warning("⚠️ Mensagem enviada mas sem ID")



    def resumo_telegram(self):
        # Adicionar timestamp no cabeçalho (convertendo UTC para Brasília UTC-3)
        agora_utc = datetime.utcnow()
        agora_brasilia = agora_utc - timedelta(hours=3)
        timestamp = agora_brasilia.strftime("%d/%m/%Y às %H:%M")
        # Filtrar apenas resultados com alerta
        resultados_com_alerta = [r for r in self.resultados if r.get("alerta")]
        
        if not resultados_com_alerta:
            return f"🛫 <b>VaiPromo Monitor</b>\n🕒 <i>Atualizado em {timestamp}</i>\n\n✅ Nenhum preço abaixo do alerta configurado."
        
        linhas = ["🛫 <b>VaiPromo Monitor</b>", f"🕒 <i>Atualizado em {timestamp} (Brasília)</i>", "\n🚨 <b>ALERTAS DE PREÇO</b>"]

        for r in resultados_com_alerta:
            c = r["consulta"]
            linhas.append(f"\n<b>{c['origem']} → {c['destino']} ({c['data']})</b>")

            if "error" in r:
                linhas.append(f"❌ {r['error']}")
            else:
                # Mostrar top 3 voos
                for i, v in enumerate(r["voos"][:3]):
                    if i == 0:
                        linhas.append(f"🏆 <b>{v['companhia']}</b> – {v['preco']}")
                    else:
                        linhas.append(f"#{i+1} {v['companhia']} – {v['preco']}")
                
                linhas.append(f"🔗 <a href=\"{r['url']}\">Ver no VaiPromo</a>")

        return "\n".join(linhas)

    # =======================
    # EXECUÇÃO
    # =======================
    def executar_monitoramento(self):
        for consulta in self.config["CONSULTAS"]:
            logging.info(f"Consultando: {consulta['origem']} → {consulta['destino']}")
            self.resultados.append(self.executar_consulta(consulta))

    def executar(self):
        self.executar_monitoramento()
        self.enviar_telegram(self.resumo_telegram())
        logging.info("🎉 Execução concluída!")


# =======================
# MAIN
# =======================
def main():
    VaiPromoMonitor().executar()


if __name__ == "__main__":
    main()