import os
import shutil
from pprint import pprint
import re
import math

import PIL.Image
import PIL.ImageDraw
import PIL.ImagePath
import toolz.functoolz as tzf
import toolz.dicttoolz as tzd
import clj
import shapely
import shapely.geometry
import shapely.ops
import scipy as sp
import scipy.sparse
import scipy.sparse.csgraph


def _desc_path():
    # return './data/part-1-examples/example-01.desc'
    return './data/part-1-initial/prob-002.desc'


def _point_pattern():
    return r'\((\d+),(\d+)\)'


def _parse_map_str(map_str):
    return tzf.thread_last(re.findall(_point_pattern(), map_str),
                           (map, lambda p: tuple(int(x) for x in p)),
                           list)


def _parse_worker_pos(worker_str):
    return _parse_map_str(worker_str)[0]

def _parse_obstacles_str(s):
    return tzf.thread_last(s.split(';'),
                           (map, _parse_map_str),
                           list)


def _read_desc(path):
    with open(path) as f:
        contents = f.read()
    mine_map_str, worker_pos_str, obstacles_str, boosters_str = contents.split('#')

    return {'mine_shell': _parse_map_str(mine_map_str),
            'worker_pos': _parse_worker_pos(worker_pos_str),
            'obstacle_shells': _parse_obstacles_str(obstacles_str)}


def _draw_shell(im, pts, scale, color):
    d_ctx = PIL.ImageDraw.Draw(im)
    d_ctx.polygon([(x * scale, y * scale) for x, y in pts],
                  fill=color)


def _pt2shell(pt):
    x, y = pt
    return [(x, y),
            (x + 1, y),
            (x + 1, y + 1),
            (x, y + 1)]


def _draw_point(im, pt, scale, color):
    _draw_shell(im, _pt2shell(pt),
                scale=scale, color=color)


def _worker_reach_pts(pos, orien):
    x, y = pos
    if orien == 'r':
        return [(x, y),
                (x + 1, y - 1),
                (x + 1, y),
                (x + 1, y + 1)]
    elif orien == 'l':
        return [(x, y),
                (x - 1, y - 1),
                (x - 1, y),
                (x - 1, y + 1)]


def _draw_state(im, state, draw_opts):
    d_ctx = PIL.ImageDraw.Draw(im)

    _draw_shell(im, state['desc']['mine_shell'], scale=draw_opts['render_scale'], color='white')

    for obs_pts in state['desc']['obstacle_shells']:
        _draw_shell(im, obs_pts, scale=draw_opts['render_scale'], color='gray')

    for shell in state['wrapped_shells']:
        _draw_shell(im, shell, scale=draw_opts['render_scale'], color='silver')

    # for pt in _worker_reach_pts(state['worker']['pos'], state['worker']['orien']):
    #     _draw_point(im, pt,
    #                 scale=draw_opts['render_scale'], color='darkorange')
    _draw_point(im, state['worker']['pos'],
                scale=draw_opts['render_scale'], color='red')

    for pt in state.get('path_pts_to_not_wrapped', []):
        _draw_point(im, pt, scale=draw_opts['render_scale'], color='indigo')


def _export_im(im, path, draw_opts):
    im = im.transpose(PIL.Image.FLIP_TOP_BOTTOM)
    im.save(path)


def _tile_pt2center_pt(pt):
    x, y = pt
    return x + 0.5, y + 0.5


def _center_pt2tile_pt(pt):
    x, y = pt
    return round(x - 0.5), round(y - 0.5)


def _shapely_point2pt(point):
    x, y, *_ = point.bounds
    return x, y


def _snap_to_tile(pt):
    x, y = pt
    return math.floor(x), math.floor(y)


def _move_projection_tile(pos, move):
    x, y = pos
    if move == 'W':
        return x, y + 1
    elif move == 'S':
        return x, y - 1
    elif move == 'A':
        return x - 1, y
    elif move == 'D':
        return x + 1, y


def _move_projection_center(pos, move):
    return tzf.thread_first(pos,
                            (_move_projection_tile, move),
                            _tile_pt2center_pt,
                            lambda pt: shapely.geometry.Point(*pt))


def _incidence_ind(x, y, x_size):
    return y * x_size + x


def _incidence_pt(ind, x_size):
    return ind % x_size, ind // x_size


def _incidence_matrix(polygon):
    x_min = round(polygon.bounds[0])
    y_min = round(polygon.bounds[1])
    x_max = math.ceil(polygon.bounds[2])
    y_max = math.ceil(polygon.bounds[3])

    incidence = sp.sparse.dok_matrix((x_max * y_max, x_max * y_max), dtype=bool)
    for tile_y in range(y_min, y_max):
        for tile_x in range(x_min, x_max):
            middle_ind = _incidence_ind(tile_x, tile_y, x_size=x_max)
            for move in ['W', 'S', 'A', 'D']:
                moved_center = _move_projection_center((tile_x, tile_y), move)
                if polygon.contains(moved_center):
                    moved_tile = tzf.thread_first(moved_center,
                                                  _shapely_point2pt,
                                                  _center_pt2tile_pt)
                    moved_ind = _incidence_ind(moved_tile[0], moved_tile[1], x_size=x_max)
                    incidence[middle_ind, moved_ind] = True

    return incidence.tocsr()


def _path_inds(predecessors, start_vertex_ind):
    path = []
    next_ind = predecessors[start_vertex_ind]
    while next_ind != -9999:
        path.append(next_ind)
        next_ind = predecessors[next_ind]
    return path


def _projection_pt_move(pos_pt, proj_pt):
    if proj_pt[1] > pos_pt[1]:
        return 'W'
    elif proj_pt[1] < pos_pt[1]:
        return 'S'
    elif proj_pt[0] < pos_pt[0]:
        return 'A'
    elif proj_pt[0] > pos_pt[0]:
        return 'D'


def _predict_action(state):
    mine = shapely.geometry.Polygon(state['desc']['mine_shell'])
    obstacles = [shapely.geometry.Polygon(sh) for sh in state['desc']['obstacle_shells']]
    obstacle = shapely.ops.unary_union(obstacles)
    situable = mine.difference(obstacle)
    wrappeds = [shapely.geometry.Polygon(sh) for sh in state['wrapped_shells']]
    wrapped = shapely.ops.unary_union(wrappeds)
    not_wrapped = situable.difference(wrapped)

    last_move = state.get('last_move', 'W')
    for move in [last_move, 'W', 'S', 'A', 'D']:
        proj = _move_projection_center(state['worker']['pos'], move)
        if not_wrapped.contains(proj):
            return move, tzd.dissoc(state, 'path_pts_to_not_wrapped')

    if not state.get('path_pts_to_not_wrapped'):
        target_tile = tzf.thread_first(not_wrapped.representative_point(),
                                       _shapely_point2pt,
                                       _snap_to_tile)
        print('Finding shortest path from tile {} to {}'.format(state['worker']['pos'], target_tile))

        incidence_m = _incidence_matrix(situable)
        target_vertex_ind = _incidence_ind(target_tile[0], target_tile[1], x_size=math.ceil(situable.bounds[2]))
        path_dists, path_predecessors = sp.sparse.csgraph.shortest_path(csgraph=incidence_m,
                                                                        directed=False,
                                                                        return_predecessors=True,
                                                                        unweighted=True,
                                                                        indices=target_vertex_ind)
        start_vertex_ind = _incidence_ind(state['worker']['pos'][0],
                                          state['worker']['pos'][1],
                                          x_size=math.ceil(situable.bounds[2]))

        path_inds = _path_inds(path_predecessors, start_vertex_ind)
        path_pts = [_incidence_pt(ind, x_size=math.ceil(situable.bounds[2]))
                    for ind in path_inds]
        print('Found path: {}'.format(path_pts))
        state = tzd.assoc(state, 'path_pts_to_not_wrapped', path_pts)

    path_move = _projection_pt_move(state['worker']['pos'], state['path_pts_to_not_wrapped'][0])
    if path_move is not None:
        return path_move, tzd.update_in(state, ['path_pts_to_not_wrapped'], lambda p: p[1:])

    return 'Z', state


def _update_state(state, action):
    # TODO: simplify (join) wrapped shells
    state = tzd.update_in(state, ['wrapped_shells'],
                          lambda shs: shs + [_pt2shell(state['worker']['pos'])])

    if action == 'Z':
        return state
    elif action == 'W':
        return tzd.update_in(state, ['worker', 'pos'], lambda p: (p[0], p[1] + 1))
    elif action == 'S':
        return tzd.update_in(state, ['worker', 'pos'], lambda p: (p[0], p[1] - 1))
    elif action == 'A':
        return tzd.update_in(state, ['worker', 'pos'], lambda p: (p[0] - 1, p[1]))
    elif action == 'D':
        return tzd.update_in(state, ['worker', 'pos'], lambda p: (p[0] + 1, p[1]))


def _output_image_dir(desc_path):
    desc_name = tzf.thread_last(desc_path,
                                os.path.basename,
                                os.path.splitext)[0]
    return os.path.join('./data/output', desc_name)


def _output_image_filepath(desc_path, turn, ext='.png'):
    return os.path.join(_output_image_dir(desc_path), '{}{}'.format(turn, ext))


def _export_state(state, turn_i, desc_path, draw_opts):
    map_bbox = PIL.ImagePath.Path(state['desc']['mine_shell']).getbbox()
    map_size = [math.ceil(a * draw_opts['render_scale'])
                for a in [map_bbox[2] - map_bbox[0], map_bbox[3] - map_bbox[1]]]
    im = PIL.Image.new('RGBA', map_size)
    _draw_state(im, state, draw_opts)

    os.makedirs(_output_image_dir(desc_path), exist_ok=True)
    _export_im(im, _output_image_filepath(desc_path, turn_i), draw_opts)


def main():
    desc = _read_desc(_desc_path())
    pprint(desc)

    initial_state = {'desc': tzd.dissoc(desc, 'worker_pos'),
                     'worker': {'pos': desc['worker_pos'],
                                'orien': 'r'},
                     'wrapped_shells': []}

    states = [initial_state]
    shutil.rmtree(_output_image_dir(_desc_path()), ignore_errors=True)
    for turn_i in range(600):
        print('- turn {}'.format(turn_i))
        prev_state = states[turn_i]
        action, intermediate_state = _predict_action(prev_state)
        # if turn_i in range(196, 205):
        if turn_i in range(0, 1000):
            _export_state(intermediate_state, turn_i, _desc_path(), draw_opts={'render_scale': 10})
        next_state = _update_state(intermediate_state, action)
        states.append(next_state)


if __name__ == '__main__':
    main()
