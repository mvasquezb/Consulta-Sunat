from selenium.common.exceptions import *
from PIL import Image
import pyocr
import requests
import bs4
import re
import collections
import tempfile
from .utils import (
    CIIU,
    DeudaCoactiva,
    OmisionTributaria,
    Contribuyente
)


class InvalidRUCError(Exception):
    pass


class Sunat:
    def __init__(self, web_driver, logger):
        self.web_driver = web_driver
        self.logger = logger
        self.url_consulta = 'http://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/jcrS00Alias'

    def get_subimage(self, source, loc, size):
        img = Image.open(source)

        left = loc['x']
        top = loc['y']
        right = loc['x'] + size['width']
        bottom = loc['y'] + size['height']

        img = img.crop((left, top, right, bottom))

        return img

    def get_text_from_image(self, image):
        tools = pyocr.get_available_tools()
        if len(tools) == 0:
            raise ValueError("No OCR tool found")

        tool = tools[0]

        return tool.image_to_string(image)

    def get_captcha_image(self, frame_elem):
        img_xpath = '//img[@src="captcha?accion=image"]'
        self.web_driver.switch_to.frame(frame_elem)

        img_file = tempfile.NamedTemporaryFile()
        self.web_driver.save_screenshot(img_file.name)
        img_elem = self.web_driver.find_element_by_xpath(img_xpath)

        loc = img_elem.location
        size = img_elem.size
        captcha = self.get_subimage(img_file, loc, size)
        self.web_driver.switch_to_default_content()

        return captcha

    def get_captcha_text(self, frame_elem):
        captcha = self.get_captcha_image(frame_elem)
        return self.get_text_from_image(captcha)

    def save_results(self, fileobj):
        frame_path = '//frame[@src="frameResultadoBusqueda.html"]'
        result_frame = self.web_driver.find_element_by_xpath(frame_path)
        self.web_driver.switch_to.frame(result_frame)
        source = self.web_driver.page_source
        fileobj.write(source)
        fileobj.seek(0)
        self.web_driver.switch_to_default_content()

    def get_ruc_nombre_contribuyente(self, soup):
        """
        Gets the RUC and name (not commercial) of the taxpayer
        Any exception should propagate upwards
        """
        tag = soup.find(
            'td',
            {'class': 'bgn'},
            text=re.compile('n[ú|u]mero\s+de\s+ruc:\s+', re.IGNORECASE))
        ruc_tag = tag.find_next('td')
        text = ruc_tag.get_text()

        tokens = text.split('-')
        try:
            ruc = int(tokens[0])
        except ValueError as e:
            e.message = "Couldn't obtain RUC value from string: " + tokens[0]
            raise
        except IndexError as e:
            e.message = "Not enough tokens to get RUC: " + str(tokens)
            raise

        text = '-'.join(tokens[1:])

        return ruc, text.strip()

    def get_nombre_comercial_contribuyente(self, soup):
        tag = soup.find(
            'td',
            {'class': 'bgn'},
            text=re.compile('nombre\s+comercial:\s*', re.IGNORECASE)
        )
        nombre_tag = tag.find_next('td')
        return nombre_tag.get_text().strip()

    def get_estado_contribuyente(self, soup):
        tag = soup.find(
            'td',
            {'class': 'bgn'},
            text=re.compile('estado\s+del?\s+contribuyente:\s*', re.IGNORECASE)
        )
        estado_tag = tag.find_next('td')
        return estado_tag.get_text().strip()

    def get_condicion_contribuyente(self, soup):
        tag = soup.find(
            'td',
            {'class': 'bgn'},
            text=re.compile(
                'condici[ó|o]n\s+del\s+contribuyente:\s*',
                re.IGNORECASE
            )
        )
        cond_tag = tag.find_next('td')
        return cond_tag.get_text().strip()

    def get_ciiu_in_comments(self, soup):
        comments = soup.find_all(
            string=lambda text: isinstance(text, bs4.Comment)
        )

        ciiu = []
        indexSelect = -1
        selectEnd = False
        for index, com in enumerate(comments):
            if indexSelect == -1 and '<select name="select"' in com:
                indexSelect = index

            if indexSelect != -1 and not selectEnd:
                if '<option' in com:
                    com_soup = bs4.BeautifulSoup(com, 'lxml')
                    ciiu.append(com_soup.get_text().strip())
                elif '</select>' in com:
                    selectEnd = True

        ciiu = [CIIU.from_string(ci) for ci in ciiu]

        return ciiu

    def get_clean_ciiu_list(self, ciiu_comments, ciiu_options):
        clean_ciiu = []

        for index, ci in enumerate(ciiu_options):
            if ci not in ciiu_comments:
                ci.revision = 4
                clean_ciiu.append(ci)
        clean_ciiu += ciiu_comments
        return clean_ciiu

    def get_ciiu_contribuyente(self, soup):
        ciiu = []
        comments = self.get_ciiu_in_comments(soup)

        select = soup.find('select', {'name': 'select'})
        options = select.find_all('option')
        options = [CIIU.from_string(op.get_text()) for op in options]

        ciiu = self.get_clean_ciiu_list(comments, options)
        return ciiu

    def get_extended_info_attr(self, params, accion, func_from_row):
        if not isinstance(params, collections.Mapping):
            raise TypeError("params is not dictionary")
        if type(accion) is not str:
            raise TypeError("accion is not string")
        if not callable(func_from_row):
            raise TypeError("func_from_row is not callable")

        params['accion'] = accion
        try:
            res = requests.get(self.url_consulta, params, timeout=5)
        except requests.exceptions.Timeout as e:
            e.message = "Couldn't connect to {action} within {time} seconds".format(action=accion, time=5)
            raise
        soup = bs4.BeautifulSoup(res.text, 'lxml')

        # First table for the title, second for the results of the query
        tables = soup.find_all('table')
        results_table = tables[1]
        intro_cell = results_table.find('td', {'class': 'bgn'})

        attr_list = []
        if intro_cell.get_text().strip().startswith('No'):
            # There are no records
            return attr_list

        # There are records
        debt_table = results_table.find('table').find('table')
        # Discard header row
        rows = debt_table.find_all('tr')[1:]

        # Check if table only has error message (case with 'Actas Probatorias')
        if rows[0].find('td').get_text().strip().startswith('No'):
            return attr_list

        # Everything is fine, continue parsing
        for row in rows:
            attr_list.append(func_from_row(row))

        return attr_list

    def get_deuda_from_row(self, row):
        values = [cell.get_text().strip() for cell in row.find_all('td')]

        if len(values) != 4:
            raise ValueError(
                "Incorrect number of attributes for '{name}' record"
                .format(name='Deuda Coactiva')
            )

        monto = float(values[0])
        periodo_tributario = values[1]
        fecha = values[2]
        entidad_asociada = values[3]

        return DeudaCoactiva(
            monto,
            periodo_tributario,
            fecha,
            entidad_asociada
        )

    def get_ot_from_row(self, row):
        values = [cell.get_text().strip() for cell in row.find_all('td')]

        if len(values) != 2:
            raise ValueError("Incorrect number of attributes for '{name}' record".format(name='Omision Tributaria'))

        periodo_tributario = values[0]
        tributo = values[1]

        return OmisionTributaria(periodo_tributario, tributo)

    def get_acta_prob_from_row(self, row):
        values = [cell.get_text().strip() for cell in row.find_all('td')]

        if len(values) != 2:
            raise ValueError("Incorrect number of attributes for '{name}' record".format(name='Acta Probatoria'))

        num_acta = int(values[0])
        fecha = values[1]
        lugar = values[2]
        infraccion = values[3]
        desc_infraccion = values[4]
        ri_roz = values[5]
        acta_recon = values[6]

        return (
            num_acta,
            fecha,
            lugar,
            infraccion,
            desc_infraccion,
            ri_roz, acta_recon
        )

    def get_deuda_coactiva_contribuyente(self, params):
        deudas = self.get_extended_info_attr(
            params,
            'getInfoDC',
            self.get_deuda_from_row
        )
        return deudas

    def get_omision_tributaria_contribuyente(self, params):
        ot = self.get_extended_info_attr(
            params,
            'getInfoOT',
            self.get_ot_from_row
        )
        return ot

    def get_extended_information(self, ruc, nombre):
        """
        Get extended data
        """
        params = {
            'nroRuc': ruc,
            'desRuc': nombre,
        }
        data = {}
        data['deuda_coactiva'] = self.get_deuda_coactiva_contribuyente(params)
        data['omision_tributaria'] = self.get_omision_tributaria_contribuyente(params)
        return data

    def parse_results_file(self, fileobj):
        text = fileobj.read()
        html = bs4.BeautifulSoup(text, "lxml")

        error = html.find('p', {'class': 'error'})
        if error is not None:
            raise AttributeError(error.get_text())

        data = {}

        data['ruc'], data['nombre'] = self.get_ruc_nombre_contribuyente(html)
        data['nombre_comercial'] = self.get_nombre_comercial_contribuyente(html)
        data['estado'] = self.get_estado_contribuyente(html)
        data['condicion'] = self.get_condicion_contribuyente(html)
        data['ciiu'] = self.get_ciiu_contribuyente(html)

        return data

    def get_search_frame(self, driver):
        search_frame_xpath = '//frame[@src="frameCriterioBusqueda.jsp"]'
        try:
            search_frame = driver.find_element_by_xpath(search_frame_xpath)
            return search_frame
        except NoSuchElementException as e:
            e.msg = eval(e.msg)['errorMessage']
            raise

    def solve_captcha(self, driver):
        search_frame = self.get_search_frame(driver)
        captcha = self.get_captcha_text(search_frame)
        self.logger.info("Text in captcha: %s", captcha)
        if not captcha or len(captcha) != 4:
            raise ValueError("Error reading captcha: {}".format(captcha))
        return captcha

    def submit_search_form(self, type, value, captcha):
        search_frame = self.get_search_frame(self.web_driver)
        self.web_driver.switch_to.frame(search_frame)
        value_input = None
        type_radio = None
        if type is not 'ruc' and type is not 'name' and type is not 'dni':
            raise ValueError("Query type must be one of: ruc, name or dni")

        try:
            radio_path = '//input[@type="radio" and @name="tQuery"]'
            radio_list = self.web_driver.find_elements_by_xpath(radio_path)
            if type == 'ruc':
                ruc_path = '//input[@name="search1"]'
                value_input = self.web_driver.find_element_by_xpath(ruc_path)
                type_radio = radio_list[0]
            elif type == 'dni':
                dni_path = '//input[@name="search2"]'
                value_input = self.web_driver.find_element_by_xpath(dni_path)
                type_radio = radio_list[1]
            elif type == 'name':
                name_path = '//input[@name="search3"]'
                value_input = self.web_driver.find_element_by_xpath(name_path)
                type_radio = radio_list[2]
            captcha_path = '//input[@name="codigo"]'
            captcha_input = self.web_driver.find_element_by_xpath(captcha_path)
            submit_path = '//input[@value="Buscar"]'
            submit_btn = self.web_driver.find_element_by_xpath(submit_path)
        except NoSuchElementException as e:
            e.msg = eval(e.msg)['errorMessage']
            raise

        type_radio.click()
        value_input.send_keys(str(value))
        captcha_input.send_keys(str(captcha))
        submit_btn.click()
        self.web_driver.switch_to_default_content()

    def get_basic_information(self, ruc):
        self.web_driver.get(self.url_consulta)
        captcha = self.solve_captcha(self.web_driver)
        self.submit_search_form('ruc', ruc, captcha)

        tmp_file = tempfile.TemporaryFile(mode='w+', encoding="utf-8")
        self.save_results(tmp_file)
        data = self.parse_results_file(tmp_file)
        tmp_file.close()
        return data

    def get_all_information_util(self, ruc):
        basic_data = self.get_basic_information(ruc)
        ext_data = self.get_extended_information(ruc, basic_data['nombre'])
        data = {}
        data.update(basic_data)
        data.update(ext_data)
        return data

    def get_all_information(self, ruc):
        if not self.validate_ruc(ruc):
            raise InvalidRUCError("Invalid RUC: {ruc}".format(ruc=ruc))

        args = [ruc]
        return self.query_wrapper(self.get_all_information_util, *args)

    def get_ruc_list_in_frame(self, frame):
        return []

    def query_wrapper(self, func, *args):
        try:
            data = None
            data = func(*args)
        except TimeoutException:
            self.logger.error("Page load timed out")
            self.logger.info('Waiting before retry...')
            self.web_driver.implicitly_wait(5)
        except Exception as e:
            self.logger.error(e)
        finally:
            self.web_driver.switch_to_default_content()
        return data

    def get_ruc_list_by_name_util(self, name):
        self.web_driver.get(self.url_consulta)
        captcha = self.solve_captcha(self.web_driver)
        self.submit_search_form('name', name, captcha)
        ruc_list = self.get_ruc_list_in_frame(result_frame)
        return ruc_list

    def get_ruc_list_by_name(self, name):
        args = [name]
        return self.query_wrapper(self.get_ruc_list_by_name_util, *args)

    def validate_ruc(self, ruc):
        ruc_str = str(ruc)

        if len(ruc_str) != 11:
            return False

        prefix = ruc_str[:2]
        if prefix not in ['10', '15', '17', '20']:
            return False

        last_digit = ruc_str[-1]
        # Diez factores para multiplicar por los primeros 10 dígitos del ruc
        fixed_multipliers = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
        ten_digits = [int(num) for num in ruc_str[:10]]
        # Suma de productos de los 10 primero dígitos con los factores fijos
        weighted_sum = sum(
            ten_digits[i] * fixed_multipliers[i] for i in range(10)
        )
        # Parte entera de la sumaproducto entre los 11 dígitos del ruc
        int_avg = int(weighted_sum / len(ruc_str))
        # Número mágico que debe ser igual al último dígito del ruc
        # El 11 debe ser la longitud del ruc (no confirmado)
        magic_number = 11 - (weighted_sum - int_avg * 11)
        magic_number = int(magic_number) % 10

        return str(magic_number) == last_digit
