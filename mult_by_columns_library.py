"""Demo of how to use pandas to multiply one table by another."""
import logging
import multiprocessing
import os
import re
import sys

from osgeo import gdal
import pandas
import pygeoprocessing
import numpy
import taskgraph

gdal.SetCacheMax(2**27)

# treat this one column name as special for the y intercept
INTERCEPT_COLUMN_ID = 'intercept'
OPERATOR_FN = {
    '+': numpy.add,
    '*': numpy.multiply,
    '^': numpy.power,
}
N_CPUS = multiprocessing.cpu_count()

logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '%(asctime)s (%(relativeCreated)d) %(levelname)s %(name)s'
        ' [%(funcName)s:%(lineno)d] %(message)s'),
    stream=sys.stdout)

LOGGER = logging.getLogger(__name__)
logging.getLogger('taskgraph').setLevel(logging.INFO)


def raster_rpn_calculator_op(*args_list):
    """Calculate RPN expression.

    Args:
        args_list (list): a length list of N+4 long where:
            - the first N elements are array followed by nodata
            - the N+1th element is the target nodata
            - the N+2nd  element is an RPN stack containing either
              symbols, numeric values, or an operator in OPERATOR_SET.
            - N+3rd value is a dict mapping the symbol to a dict with
              "index" in it showing where index*2 location it is in the
              args_list.
            - N+4th value is a set of symbols that if present should set their
              nodata to 0.
            - N+5th value is "conversion factor" to multiply the final result
              by if it is not None.

    Returns:
        evaluation of the RPN calculation
    """
    n = len(args_list)-5
    result = numpy.empty(args_list[0].shape, dtype=numpy.float32)
    result[:] = args_list[n]  # target nodata
    rpn_stack = list(args_list[n+1])
    info_dict = args_list[n+2]
    zero_nodata_indexes = args_list[n+3]
    conversion_factor = args_list[n+4]

    if conversion_factor is None:
        conversion_factor = 1

    valid_mask = numpy.ones(args_list[0].shape, dtype=numpy.bool)
    # build up valid mask where all pixel stacks are defined
    for index in range(0, n, 2):
        nodata_value = args_list[index+1]
        if nodata_value is not None and index//2 not in zero_nodata_indexes:
            valid_mask &= \
                ~numpy.isclose(args_list[index], args_list[index+1])

    # process the rpn stack
    accumulator_stack = []
    while rpn_stack:
        val = rpn_stack.pop(0)
        if val in OPERATOR_FN:
            operator = val
            operand_b = accumulator_stack.pop()
            operand_a = accumulator_stack.pop()
            val = OPERATOR_FN[operator](operand_a, operand_b)
            accumulator_stack.append(val)
        else:
            if isinstance(val, str):
                arg_index = info_dict[val]['index']
                if arg_index in zero_nodata_indexes:
                    nodata_mask = numpy.isclose(
                        args_list[2*arg_index],  args_list[2*arg_index+1])
                    args_list[2*arg_index][nodata_mask] = 0.0
                accumulator_stack.append(args_list[2*arg_index][valid_mask])
            else:
                accumulator_stack.append(val)

    result[valid_mask] = accumulator_stack.pop(0) * conversion_factor
    if accumulator_stack:
        raise RuntimeError(
            f'accumulator_stack not empty: {accumulator_stack}')
    return result


def mult_by_columns(
        lasso_table_path, data_dir, workspace_dir,
        base_convolution_raster_id, target_raster_id, bounding_box,
        pixel_size, target_result_path, task_graph,
        zero_nodata_symbols=None, target_nodata=numpy.finfo('float32').min,
        conversion_factor=None):
    """Calculate large regression.

    Args:
        lasso_table_path (str): path to lasso table
        data_dir (str): path to directory containing rasters in lasso
            table path
        workspace_dir (str): path to output directory, will contain
            "result.tif" after completion
        base_convolution_raster_id (str): The convolution columns in
            the lasso table have the form  [base]_[mask_type]_gs[kernel_size],
            this parameter matches [base] so it can be replaced with a
            filename of the form [target_raster_id]_[mask_type]_[kernel_size].
        target_raster_id (str): this is the base of the target raster that
            to use in the table.
        bounding_box (list): If not `None`, manual bounding box in the form
            of four  consecutive floats: "min_lng, min_lat, max_lng,
            max_lat, ex: " "-180.0, -58.3, 180.0, 81.5".
        pixel_size (tuple): desired target pixel size in raster units
        target_result_path (str): path to desired output raster
        task_graph (TaskGraph): TaskGraph object that can be used for
            scheduling.
        zero_nodata_symbols (set): set of symbols whose nodata values should be
            treated as 0.
        target_nodata (float): desired target nodata value
        conversion_factor (float): if not None, this factor is multiplied by
            the final result befor going into target

    Returns:
        None

    """
    lasso_df = pandas.read_csv(lasso_table_path, header=None)
    LOGGER.debug(f"parsing through {lasso_table_path}")
    # built a reverse polish notation stack for the operations and their order
    # that they need to be executed in
    rpn_stack = []
    first_term = True
    for row_index, row in lasso_df.iterrows():
        header = row[0]
        if header == INTERCEPT_COLUMN_ID:
            # special case of the intercept, just push it
            rpn_stack.append(float(row[1]))
        else:
            # it's an expression/coefficient row
            LOGGER.debug(f'{row_index}: {row}')
            coefficient = float(row[1])
            # put on the coefficient first since it's there, we'll multiply
            # it later
            rpn_stack.append(coefficient)

            # split out all the multiplcation terms
            product_list = header.split('*')
            for product in product_list:
                if product.startswith(base_convolution_raster_id):
                    LOGGER.debug(f'parsing out base and gs in {product}')
                    match = re.match(
                        fr'{base_convolution_raster_id}(.*)',
                        product)
                    suffix = match.group(1)
                    product = \
                        f'''{target_raster_id}{suffix}'''
                # for each multiplication term split out an exponent if exists
                if '^' in product:
                    rpn_stack.extend(product.split('^'))
                    # cast the exponent to an integer so can operate directly
                    rpn_stack[-1] = int(rpn_stack[-1])
                    # push the ^ to exponentiate the last two operations
                    rpn_stack.append('^')
                else:
                    # otherwise it's a single value
                    rpn_stack.append(product)
                # multiply this term and the last
                rpn_stack.append('*')

        # if it's not the first term we want to add the rest
        if first_term:
            first_term = False
        else:
            rpn_stack.append('+')

    LOGGER.debug(rpn_stack)

    # find the unique symbols in the expression
    raster_id_list = [
        x for x in set(rpn_stack)-set(OPERATOR_FN)
        if not isinstance(x, (int, float))]

    LOGGER.debug(raster_id_list)

    # translate symbols into raster paths and get relevant raster info
    raster_id_to_info_map = {}
    missing_raster_path_list = []
    min_size = sys.float_info.max
    bounding_box_list = []
    for index, raster_id in enumerate(raster_id_list):
        raster_path = os.path.join(data_dir, f'{raster_id}.tif')
        if not os.path.exists(raster_path):
            missing_raster_path_list.append(raster_path)
            continue
        else:
            raster_info = pygeoprocessing.get_raster_info(raster_path)
            raster_id_to_info_map[raster_id] = {
                'path': raster_path,
                'nodata': raster_info['nodata'][0],
                'index': index,
            }
            min_size = min(
                min_size, abs(raster_info['pixel_size'][0]))
            bounding_box_list.append(raster_info['bounding_box'])

    if missing_raster_path_list:
        LOGGER.error(
            f'expected the following '
            f'{"rasters" if len(missing_raster_path_list) > 1 else "raster"} given '
            f'the entries in the table, but could not find them locally:\n'
            + "\n".join(missing_raster_path_list))
        sys.exit(-1)

    LOGGER.info(
        f'raster paths:\n{str(raster_id_to_info_map)}')

    if bounding_box:
        target_bounding_box = bounding_box
    else:
        target_bounding_box = pygeoprocessing.merge_bounding_box_list(
            bounding_box_list, 'intersection')

    if not pixel_size:
        pixel_size = (min_size, -min_size)

    LOGGER.info(f'target pixel size: {pixel_size}')
    LOGGER.info(f'target bounding box: {target_bounding_box}')

    LOGGER.debug('align rasters, this might take a while')
    align_dir = os.path.join(workspace_dir, 'aligned_rasters')
    try:
        os.makedirs(align_dir)
    except OSError:
        pass

    # align rasters and cast to list because we'll rewrite
    # raster_id_to_path_map object
    for raster_id in raster_id_to_info_map:
        raster_path = raster_id_to_info_map[raster_id]['path']
        raster_basename = os.path.splitext(os.path.basename(raster_path))[0]
        aligned_raster_path = os.path.join(
            align_dir,
            f'{raster_basename}_{target_bounding_box}_{pixel_size}.tif')
        raster_id_to_info_map[raster_id]['aligned_path'] = \
            aligned_raster_path
        task_graph.add_task(
            func=pygeoprocessing.warp_raster,
            args=(
                raster_path, pixel_size, aligned_raster_path,
                'near'),
            kwargs={
                'target_bb': target_bounding_box,
                'working_dir': workspace_dir
            })

    LOGGER.info('construct raster calculator raster path band list')
    raster_path_band_list = []
    LOGGER.debug(raster_id_list)
    LOGGER.debug(raster_id_to_info_map)
    for index, raster_id in enumerate(raster_id_list):
        raster_path_band_list.append(
            (raster_id_to_info_map[raster_id]['aligned_path'], 1))
        raster_path_band_list.append(
            (raster_id_to_info_map[raster_id]['nodata'], 'raw'))
        if index != raster_id_to_info_map[raster_id]['index']:
            raise RuntimeError(
                f"indexes dont match: {index} {raster_id} "
                f"{raster_id_to_info_map}")

    zero_nodata_indexes = {
        raster_id_to_info_map[raster_id]['index']
        for raster_id in zero_nodata_symbols
        if raster_id in raster_id_to_info_map}

    raster_path_band_list.append((target_nodata, 'raw'))
    raster_path_band_list.append((rpn_stack, 'raw'))
    raster_path_band_list.append((raster_id_to_info_map, 'raw'))
    raster_path_band_list.append((zero_nodata_indexes, 'raw'))
    raster_path_band_list.append((conversion_factor, 'raw'))
    LOGGER.debug(rpn_stack)

    # wait for rasters to align
    task_graph.join()

    LOGGER.debug(raster_path_band_list)
    pygeoprocessing.raster_calculator(
        raster_path_band_list, raster_rpn_calculator_op, target_result_path,
        gdal.GDT_Float32, float(target_nodata))
    LOGGER.debug('all done with mult by raster')
