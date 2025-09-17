"""
VaiPromo Monitor - Versão ajustada para rodar em GitHub Actions e salvar relatorio.html em docs/
"""

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')


class VaiPromoMonitor:
    def __init__(self):
        self.config = self.carregar_config()
        self.resultados = []
        self.vaidepromo_url = "https://www.vaidepromo.com.br/passagens-aereas/"
        # tempo em ms para page.wait_for_timeout (Playwright usa ms)
        self.tempo_espera = 10000

    def carregar_config(self):
        """Carrega configurações do config.json"""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            logging.info(f"Configuração carregada: {len(config.get('CONSULTAS', []))} consultas")
            return config
        except FileNotFoundError:
            logging.error("config.json não encontrado. Crie um config.json com chave 'CONSULTAS'.")
            raise
        except Exception as e:
            logging.error(f"Erro ao carregar config.json: {e}")
            raise

    def trigger_change_events(self, page, selector):
        """Dispara eventos de mudança no elemento"""
        page.evaluate(f'''() => {{
            const input = document.querySelector('{selector}');
            if (input) {{
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            }}
        }}''')

    def preencher_localizacao(self, page, data_cy, sigla):
        """Preenche campo de localização (origem/destino)"""
        page.locator(f'[data-cy="{data_cy}"]').click()
        page.locator(f'[data-cy="{data_cy}"]').fill(sigla)
        page.wait_for_selector(f'[role="option"]:has-text("{sigla}")', timeout=3000)
        page.locator(f'[role="option"]:has-text("{sigla}")').first.click()
        self.trigger_change_events(page, f'[data-cy="{data_cy}"]')
        page.wait_for_function(
            f'document.querySelector("[data-cy=\\\"{data_cy}\\\"]").value.includes("{sigla}")',
            timeout=3000
        )

    def navegar_para_data(self, page, data_str):
        """Navega para a data desejada no calendário"""
        data_desejada = datetime.strptime(data_str, "%d/%m/%Y")

        def obter_data_calendario():
            mes_str = page.query_selector("div[class*='monthTitle'] strong").inner_text()
            ano_str = page.query_selector("div[class*='monthTitle'] span").inner_text()
            mes_map = {
                "Janeiro": 1, "Fevereiro": 2, "Março": 3, "Abril": 4,
                "Maio": 5, "Junho": 6, "Julho": 7, "Agosto": 8,
                "Setembro": 9, "Outubro": 10, "Novembro": 11, "Dezembro": 12
            }
            return datetime(int(ano_str), mes_map[mes_str], 1)

        page.wait_for_selector("div[class*='monthTitle']", timeout=5000)

        # Navegar até o mês correto (avança mês enquanto data atual < desejada)
        for _ in range(12):  # limite de iterações para evitar loop infinito
            data_atual = obter_data_calendario()
            if (data_atual.year > data_desejada.year) or \
               (data_atual.year == data_desejada.year and data_atual.month >= data_desejada.month):
                break
            page.locator('button[data-cy="data-range-picker-next"]').first.click()
            page.wait_for_timeout(300)

        # Clicar na data
        data_formatada = data_desejada.strftime("%d-%m-%Y")
        date_selector = f'button[data-cy="{data_formatada}"]'
        page.wait_for_selector(date_selector, timeout=5000)
        page.evaluate(f'''() => {{
            const dateButton = document.querySelector('{date_selector}');
            if (dateButton) dateButton.scrollIntoView({{ behavior: "smooth", block: "center" }});
        }}''')
        page.locator(date_selector).first.click(force=True)

    def wait_for_all_results(self, page, timeout=30):
        """Aguarda todos os resultados carregarem"""
        start = time.time()
        last_count = 0

        while time.time() - start < timeout:
            blocks = page.locator("div._container_m3tu2_1").all()
            count = len(blocks)

            if count == last_count and count > 0:
                time.sleep(1)
                blocks2 = page.locator("div._container_m3tu2_1").all()
                if len(blocks2) == count:
                    break

            last_count = count
            time.sleep(1)

    def extrair_voos(self, page):
        """Extrai informações dos voos da página"""
        voos = []
        price_blocks = page.locator("div._container_m3tu2_1").all()

        for idx, price_block in enumerate(price_blocks):
            try:
                # Sobe para o bloco pai (resultado de voo)
                parent = price_block.locator("xpath=ancestor::div[contains(@class, '_content_zq77q_1')]").first

                # Busca a companhia dentro do bloco pai
                companhias = parent.locator("div._iataInfo_816x7_1 span").all()
                companhia = "Companhia não encontrada"
                if companhias:
                    companhia = companhias[0].inner_text().strip()

                # Busca o preço
                strongs = price_block.locator("div._totalContainerFinalPrice_m3tu2_298 strong").all()
                preco = "Preço não encontrado"
                if len(strongs) >= 2:
                    preco = strongs[1].inner_text().replace("\u00a0", " ")
                elif len(strongs) == 1:
                    preco = strongs[0].inner_text().replace("\u00a0", " ")

                voos.append({"companhia": companhia, "preco": preco})

            except Exception as e:
                logging.warning(f"Erro ao extrair voo {idx}: {e}")
                voos.append({"companhia": "Erro", "preco": str(e)})

        return voos

    def executar_consulta(self, consulta):
        """Executa uma consulta específica"""
        origem = consulta['origem']
        destino = consulta['destino']
        data = consulta['data']

        logging.info(f"Consultando {origem} → {destino} em {data}")

        resultado = {
            'consulta': consulta,
            'timestamp': datetime.now().isoformat(),
            'voos': []
        }

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                # Navegar para VaiPromo
                page.goto(self.vaidepromo_url)

                # Configurar busca
                page.get_by_role("button", name="Só ida ou volta").click()
                self.preencher_localizacao(page, 'departure', origem)
                self.preencher_localizacao(page, 'arrival', destino)
                page.get_by_role("textbox", name="Ida").nth(1).click()
                self.navegar_para_data(page, data)
                page.get_by_role("button", name="Encontrar voos").click()

                # Aguardar resultados (ms)
                page.wait_for_timeout(self.tempo_espera)

                # Rolar página para carregar todos os resultados
                for _ in range(5):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2)

                # Aguardar carregamento completo
                self.wait_for_all_results(page)

                # Extrair voos
                voos = self.extrair_voos(page)
                resultado['voos'] = voos
                resultado['url'] = page.url

                logging.info(f"Encontrados {len(voos)} voos")

                context.close()
                browser.close()

        except Exception as e:
            error_msg = f"Erro na consulta {origem} → {destino}: {str(e)}"
            logging.error(error_msg)
            resultado['error'] = error_msg

        return resultado

    def executar_monitoramento(self):
        """Executa todas as consultas configuradas"""
        logging.info("Iniciando monitoramento VaiPromo")

        consultas = self.config.get('CONSULTAS', [])
        for i, consulta in enumerate(consultas, 1):
            logging.info(f"Consulta {i}/{len(consultas)}")
            resultado = self.executar_consulta(consulta)
            self.resultados.append(resultado)

            # Delay entre consultas (exceto na última)
            if i < len(consultas):
                delay = self.config.get('DELAY_ENTRE_CONSULTAS', 5)
                logging.info(f"Aguardando {delay}s...")
                time.sleep(delay)

        logging.info(f"Monitoramento concluído: {len(self.resultados)} consultas")

    def gerar_relatorio_html(self):
        """Gera relatório HTML simples"""
        agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>VaiPromo Monitor</title>
    <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; }}
    h1 {{ color: #333; }}
    .consulta {{ border: 1px solid #ddd; margin: 20px 0; padding: 15px; }}
    .voo {{ background: #f5f5f5; margin: 10px 0; padding: 10px; }}
    .error {{ background: #ffebee; color: #c62828; }}
    .melhor {{ background: #e8f5e8; border-left: 4px solid #4caf50; }}
    </style>
</head>
<body>
    <h1>VaiPromo Monitor - Resultados</h1>
    <p>Relatório gerado em: {agora.strftime('%d/%m/%Y %H:%M:%S')}</p>
    <p>Total de consultas: {len(self.resultados)}</p>
    <p>Total de voos: {sum(len(r.get('voos', [])) for r in self.resultados)}</p>
"""
        for resultado in self.resultados:
            consulta = resultado['consulta']
            descricao = consulta.get('descricao', '')

            html += f"""
    <div class="consulta">
    <h2>{consulta['origem']} → {consulta['destino']} - {consulta['data']}</h2>
    {f'<p><em>{descricao}</em></p>' if descricao else ''}
"""

            if 'error' in resultado:
                html += f'    <div class="voo error">❌ Erro: {resultado["error"]}</div>\n'
            else:
                voos = resultado.get('voos', [])
                if voos:
                    # Destacar o voo mais barato (presumindo primeiro)
                    for i, voo in enumerate(voos):
                        classe = 'melhor' if i == 0 else ''
                        destaque = '🏆 ' if i == 0 else ''
                        html += f'    <div class="voo {classe}">{destaque}{voo["companhia"]}: {voo["preco"]}</div>\n'
                else:
                    html += '    <div class="voo">Nenhum voo encontrado</div>\n'

                if 'url' in resultado:
                    html += f'    <p><a href="{resultado["url"]}" target="_blank">🔗 Ver no VaiPromo</a></p>\n'

            html += '    </div>\n'

        html += """
</body>
</html>"""
        return html

    def salvar_local(self):
        """Salva HTML localmente"""
        try:
            html = self.gerar_relatorio_html()
            with open('relatorio.html', 'w', encoding='utf-8') as f:
                f.write(html)
            logging.info("Relatório salvo localmente: relatorio.html")
        except Exception as e:
            logging.error(f"Erro ao salvar arquivo local: {e}")

    def executar(self):
        """Método principal"""
        try:
            self.executar_monitoramento()
            self.salvar_local()

            total_voos = sum(len(r.get('voos', [])) for r in self.resultados)
            sucessos = len([r for r in self.resultados if 'error' not in r])

            logging.info("🎉 Execução concluída!")
            logging.info(f"📊 {len(self.resultados)} consultas, {sucessos} sucessos, {total_voos} voos")

        except Exception as e:
            logging.error(f"Erro na execução: {e}")
            raise


def main():
    """Função principal"""
    try:
        monitor = VaiPromoMonitor()
        monitor.executar()
    except KeyboardInterrupt:
        logging.info("Execução interrompida pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal: {e}")
        exit(1)


if __name__ == "__main__":
    main()
