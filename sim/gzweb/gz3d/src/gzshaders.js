/**
 * @constructor
 * Holds custom shaders in string format which can be passed to
 * THREE.ShaderMaterial's options.
 */
GZ3D.Shaders = function()
{
  this.init();
};

GZ3D.Shaders.prototype.init = function()
{
  // Custom vertex shader for heightmaps
  this.heightmapVS =
    'varying vec2 vUv;'+
    'varying vec3 vPosition;'+
    'varying vec3 vNormal;'+
    'varying float vFogDepth;'+
    'void main( void ) {'+
    '  vUv = uv;'+
    '  vPosition = position;'+
    '  vNormal = -normal;'+
    // Camera-space depth, for fog in the fragment shader.
    '  vFogDepth = -( modelViewMatrix * vec4( position, 1.0 ) ).z;'+
    '  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);'+
    '}';

  // Custom fragment shader for heightmaps
  this.heightmapFS =
    'uniform sampler2D texture0;'+
    'uniform sampler2D texture1;'+
    'uniform sampler2D texture2;'+
    'uniform float repeat0;'+
    'uniform float repeat1;'+
    'uniform float repeat2;'+
    'uniform float minHeight1;'+
    'uniform float minHeight2;'+
    'uniform float fadeDist1;'+
    'uniform float fadeDist2;'+
    'uniform vec3 ambient;'+
    'uniform vec3 lightDiffuse;'+
    'uniform vec3 lightDir;'+
    'uniform float detailAmount;'+
    'uniform float fogDensity;'+
    'uniform vec3 fogColor;'+
    'varying vec2 vUv;'+
    'varying vec3 vPosition;'+
    'varying vec3 vNormal;'+
    'varying float vFogDepth;'+
    'float blend(float distance, float fadeDist) {'+
    '  float alpha = distance / fadeDist;'+
    '  if (alpha < 0.0) {'+
    '    alpha = 0.0;'+
    '  }'+
    '  if (alpha > 1.0) {'+
    '    alpha = 1.0;'+
    '  }'+
    '  return alpha;'+
    '}'+
    'void main()'+
    '{'+
    '  vec3 diffuse0 = texture2D( texture0, vUv*repeat0 ).rgb;'+
    '  vec3 diffuse1 = texture2D( texture1, vUv*repeat1 ).rgb;'+
    '  vec3 diffuse2 = texture2D( texture2, vUv*repeat2 ).rgb;'+
    '  vec3 fragcolor = diffuse0;'+
    '  if (fadeDist1 > 0.0)'+
    '  {'+
    '    fragcolor = mix('+
    '      fragcolor,'+
    '      diffuse1,'+
    '      blend(vPosition.z - minHeight1, fadeDist1)'+
    '    );'+
    '  }'+
    // PATCHED for arctic-sim: texture2 is a tiling DETAIL map multiplied in,
    // not a third height-blended layer. Satellite imagery bottoms out at 10 m,
    // so at low altitude it is magnified ~200x into mush; a high-frequency
    // detail tile is the only way to keep the ground readable up close. It
    // averages to ~1.0 at distance, so the wide view is unchanged.
    '  vec3 detail = texture2D( texture2, vUv*repeat2 ).rgb;'+
    '  float detailLum = dot(detail, vec3(0.299, 0.587, 0.114));'+
    '  fragcolor *= (1.0 - detailAmount) + detailAmount * 2.0 * detailLum;'+
    '  vec3 lightDirNorm = normalize(lightDir);'+
    '  float intensity = max(dot(vNormal, lightDirNorm), 0.0);'+
    '  vec3 vLightFactor = min(ambient + lightDiffuse * intensity, vec3(1.0));'+
    '  vec3 lit = fragcolor.rgb * vLightFactor;'+
    // exp2 fog, matching Gazebo's own falloff so both views agree.
    '  if (fogDensity > 0.0) {'+
    '    float fd = fogDensity * vFogDepth;'+
    '    float f = 1.0 - exp( -fd * fd );'+
    '    lit = mix( lit, fogColor, clamp( f, 0.0, 1.0 ) );'+
    '  }'+
    '  gl_FragColor = vec4(lit, 1.0);'+
    '}';
};
